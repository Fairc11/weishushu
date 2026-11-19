"""微博书归档路由。

``/api/backup/inspect`` 只读识别目录；``/api/backup/start`` 创建、增量同步
或重建微博书。``target_uid`` 缺省为当前登录账号（本人），提供时为备份
指定博主的公开微博（同一管线、同一「昵称_UID」目录规则）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.app.schemas import (
    ArchiveFolderInspection,
    ArchiveInspectRequest,
    ArchivePreviewRequest,
    ArchivePreviewStartResponse,
    ArchiveSettingsResponse,
    ArchiveSettingsUpdateRequest,
    BackupIndex,
    FollowingArchiveRequest,
    FollowingArchiveStartResponse,
    FollowingDurationCheckRequest,
    FollowingDurationCheckResponse,
    PersonalArchiveRequest,
    PersonalArchiveStartResponse,
    WhoamiResponse,
    ArchiveLibraryEntry,
    ArchiveLibraryResponse,
    VideoQualityCheckResponse,
    VideoQualityCheckRequest,
)
from backend.app.services.archive_folder import (
    find_child_archives,
    inspect_archive_folder,
    inspect_selected_folder,
    resolve_archive_dir,
)
from backend.app.services.backup_index import (
    INDEX_FILENAME,  # noqa: F401
    _validate_output_dir,  # noqa: F401
    cleanup_legacy_audit,
    finalize_legacy_archive,
    read_index,  # noqa: F401
    restore_legacy_archive,
    stage_legacy_archive,
    write_index,
)
from backend.app.services.task_manager import run_in_background, task_manager
from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
from backend.app.services.whoami import whoami
from weibo_book import WeiboBook
from weibo_book.archive.source import ArchiveMediaStager, WeiboArchiveSource
from weibo_book.archive.render_snapshot import ArchiveRenderer
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.following_sync import check_following_duration
from weibo_book.archive.pacing import AdaptiveRequestScheduler
from weibo_book.archive.sync import PersonalArchiveSync, _archive_lock, _physical_root
from weibo_book.errors import (
    OperationCancelled,
    OperationPaused,
    WeiboError,
    WeiboErrorKind,
)
from weibo_book.extractor import WeiboExtractor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backup"])


class _FixedIdentityProvider:
    """将路由已验证的身份交给同步器，避免后台二次联网。"""

    def __init__(self, identity: dict[str, str]) -> None:
        self._identity = identity

    def whoami(self) -> dict[str, str]:
        return dict(self._identity)


def build_personal_archive_dependencies(
    self_uid: str,
    *,
    pacing_scheduler: AdaptiveRequestScheduler | None = None,
    login_uid: str | None = None,
) -> tuple[Any, Any]:
    """使用现有登录 Cookie、抓取器和媒体下载器构造生产依赖。

    ``self_uid`` 是归档目标 UID；``login_uid`` 是当前登录账号（缺省等于目标，
    即本人模式）。两者不一致时构造他人模式数据源：抓取范围锁定目标 UID，
    唤醒复检只要求登录态有效。
    """
    book = WeiboBook()
    cookie_str = book.ensure_login(force=False)
    if not cookie_str:
        raise WeiboError("未登录或登录态已过期", kind=WeiboErrorKind.AUTH)
    if login_uid is None:
        login_uid = self_uid
    extractor_options: dict[str, object] = {
        "cookie_str": cookie_str,
        "image_quality": book.image_quality,
    }
    if pacing_scheduler is not None and pacing_scheduler.is_low_intensity:
        extractor_options["low_intensity"] = True
    extractor = WeiboExtractor(**extractor_options)
    return (
        WeiboArchiveSource(
            extractor,
            self_uid=login_uid,
            target_uid=None if login_uid == self_uid else self_uid,
            image_quality=book.image_quality,
            pacing_scheduler=pacing_scheduler,
        ),
        ArchiveMediaStager(
            image_quality=book.image_quality,
            pacing_scheduler=pacing_scheduler,
        ),
    )


def render_personal_archive(
    output_dir: str,
    uid: str,
    cancel_requested,
    begin_commit=None,
    pause_requested=None,
    progress_callback=None,
    timings=None,
) -> list[str]:
    """从已提交的归档数据生成固定成品，并返回相对于归档根目录的路径。"""
    root = _physical_root(Path(output_dir))
    with _archive_lock(root):
        if pause_requested is not None and pause_requested():
            raise OperationPaused("任务已暂停")
        if cancel_requested():
            raise OperationCancelled("任务已取消")
        repository = ArchiveRepository.open(root, uid)
        try:
            formats = repository.read_output_formats()
            rendered = ArchiveRenderer(repository).render_all(
                root,
                cancel_requested=cancel_requested,
                pause_requested=pause_requested,
                begin_commit=begin_commit,
                include_pdf=formats["pdf"],
                include_markdown=formats["markdown"],
                progress_callback=progress_callback,
                timings=timings,
            )
        finally:
            repository.close()
    return [path.relative_to(root).as_posix() for path in rendered.values()]


def _http_identity() -> dict[str, Any]:
    try:
        identity = whoami()
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    if not isinstance(identity, dict):
        raise HTTPException(status_code=400, detail="当前登录账号信息无效：UID 和昵称必须是非空字符串")
    uid = identity.get("uid")
    screen_name = identity.get("screen_name")
    if (
        not isinstance(uid, str)
        or not uid.strip()
        or not isinstance(screen_name, str)
        or not screen_name.strip()
    ):
        raise HTTPException(status_code=400, detail="当前登录账号信息无效：UID 和昵称必须是非空字符串")
    return identity


_UID_PATTERN = re.compile(r"^\d{5,20}$")


def _resolve_target_identity(req_target_uid: str | None, *, require_verified: bool = False) -> dict[str, str] | None:
    """``target_uid`` 提供时返回目标博主身份；等于登录账号或缺省时返回 None（本人模式）。"""
    if req_target_uid is None:
        if require_verified:
            raise HTTPException(status_code=400, detail="请先选择带认证 V 标识的博主")
        return None
    target_uid = req_target_uid.strip()
    if not _UID_PATTERN.fullmatch(target_uid):
        raise HTTPException(status_code=400, detail="目标博主 UID 无效")
    login_uid = _http_identity()["uid"]
    if target_uid == login_uid:
        if require_verified:
            raise HTTPException(status_code=400, detail="此超话补漏入口用于备份其他认证博主")
        return None
    book = WeiboBook()
    cookie_str = book.ensure_login(force=False)
    if not cookie_str:
        raise HTTPException(status_code=401, detail="未登录或登录态已过期")
    extractor = WeiboExtractor(cookie_str=cookie_str)
    try:
        info = extractor.get_user_info(target_uid)
    except WeiboError as exc:
        status = 404 if exc.kind == WeiboErrorKind.NOT_FOUND else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if require_verified and info.verified is not True:
        raise HTTPException(status_code=400, detail="超话补漏仅适用于带认证 V 标识的博主")
    if not info.uid or not info.screen_name:
        raise HTTPException(status_code=400, detail="目标博主信息无效")
    return {"uid": str(info.uid), "screen_name": info.screen_name, "login_uid": login_uid}


class _ProgressReporter:
    """从同步工作线程向 TaskManager 发送结构化进度。"""

    def __init__(
        self,
        task_id: str,
        loop: asyncio.AbstractEventLoop,
        file_total: int = 4,
    ) -> None:
        self.task_id = task_id
        self.loop = loop
        self.file_total = file_total
        self.started_at = time.monotonic()
        self._pending: list[Any] = []
        self._last_pct = 0.0

    def emit(self, event: dict) -> None:
        normalized = dict(event)
        if normalized.get("phase") == "complete":
            normalized.update({
                "phase": "generate",
                "pct": 0.96,
                "detail": "归档数据已完成，正在生成固定文件与离线数据",
                "current": 0,
                "total": self.file_total,
                "unit": "file",
            })
        self._queue(normalized)

    def emit_render_complete(self) -> None:
        self._queue({
            "phase": "complete",
            "pct": 1.0,
            "detail": "微博书归档与固定文件已完成",
            "current": self.file_total,
            "total": self.file_total,
            "unit": "file",
        })

    def _queue(self, normalized: dict) -> None:
        pct = float(normalized["pct"])
        self._last_pct = max(self._last_pct, pct)
        normalized["pct"] = self._last_pct
        normalized["elapsed_seconds"] = time.monotonic() - self.started_at
        self._pending.append(
            asyncio.run_coroutine_threadsafe(
                task_manager.update_progress_event(self.task_id, normalized), self.loop
            )
        )

    async def drain(self) -> None:
        """等待工作线程已投递的进度，防止终态后倒退为 running。"""
        pending = list(self._pending)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending)
            )


async def _await_worker(func, *args, **kwargs):
    """协程取消后等待已启动的工作线程在安全检查点结束。"""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(worker)
        except OperationCancelled:
            pass
        except Exception:
            logger.exception("任务取消后工作线程异常结束")
        raise


async def _stage_legacy_safely(output_dir: str, uid: str, task_id: str) -> Path:
    """取消发生在旧目录暂存期间时，等待暂存结束并恢复原目录。"""
    worker = asyncio.create_task(
        asyncio.to_thread(stage_legacy_archive, output_dir, uid, task_id)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        staged: Path | None = None
        try:
            staged = await asyncio.shield(worker)
        except Exception:
            logger.exception("任务取消后旧版备份暂存异常结束")
        if staged is not None:
            restore_worker = asyncio.create_task(
                asyncio.to_thread(
                    restore_legacy_archive,
                    output_dir,
                    staged,
                    task_id,
                    uid,
                )
            )
            await asyncio.shield(restore_worker)
        raise


def _ensure_mode_allowed(mode: str, inspection: ArchiveFolderInspection) -> None:
    if inspection.state == "uid_mismatch":
        raise HTTPException(status_code=409, detail="该微博书属于其他账号，不允许覆盖")
    if inspection.state == "ordinary_nonempty":
        raise HTTPException(status_code=409, detail="所选路径是非空目录，请选择空目录")
    if inspection.state == "damaged":
        raise HTTPException(status_code=409, detail="所选微博书已损坏，请先处理目录问题")
    if inspection.state == "legacy_index" and mode != "create":
        raise HTTPException(status_code=409, detail="旧版索引目录需要首次建立完整档案")
    if mode == "create" and inspection.state not in {"empty", "legacy_index"}:
        raise HTTPException(status_code=409, detail="首次建立微博书只能使用空目录")
    if mode in {"incremental", "rebuild", "backfill"} and inspection.state != "archive":
        raise HTTPException(status_code=409, detail="增量同步、重建或历史回填必须使用现有微博书目录")
    if mode == "backfill" and inspection.history_complete:
        raise HTTPException(status_code=409, detail="该微博书历史已补全，无需回填")


@router.get("/api/login/whoami", response_model=WhoamiResponse)
async def api_whoami() -> WhoamiResponse:
    return WhoamiResponse(**_http_identity())


class IndexWriteBody(BaseModel):
    index: BackupIndex


@router.post("/api/backup/index")
async def api_backup_index(
    path: str = Query(..., description="output_dir 绝对路径"),
    body: Optional[IndexWriteBody] = None,
) -> dict:
    selected = _validate_output_dir(path)
    selected.mkdir(parents=True, exist_ok=True)
    if body is not None:
        try:
            write_index(selected, body.index)
            return {"exists": True, "index": body.index.model_dump()}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写索引失败: {exc}") from exc
    index = read_index(selected)
    if index is None:
        return {"exists": False, "index": BackupIndex(uid="", bids=[]).model_dump()}
    return {"exists": True, "index": index.model_dump()}


@router.post("/api/backup/inspect", response_model=ArchiveFolderInspection)
async def api_backup_inspect(req: ArchiveInspectRequest) -> ArchiveFolderInspection:
    target = await asyncio.to_thread(_resolve_target_identity, req.target_uid)
    uid = target["uid"] if target is not None else _http_identity()["uid"]
    return await asyncio.to_thread(
        inspect_selected_folder,
        req.output_dir,
        uid,
        inspector=inspect_archive_folder,
    )


@router.post("/api/backup/start", response_model=PersonalArchiveStartResponse)
async def api_backup_start(req: PersonalArchiveRequest) -> PersonalArchiveStartResponse:
    if req.include_supertopics:
        target = await asyncio.to_thread(_resolve_target_identity, req.target_uid, require_verified=True)
    else:
        target = await asyncio.to_thread(_resolve_target_identity, req.target_uid)
    identity = target if target is not None else _http_identity()
    try:
        effective_dir = await asyncio.to_thread(
            resolve_archive_dir,
            req.output_dir,
            identity["uid"],
            identity["screen_name"],
            inspector=inspect_archive_folder,
        )
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    inspection = await asyncio.to_thread(
        inspect_archive_folder, effective_dir, current_uid=identity["uid"]
    )
    _ensure_mode_allowed(req.mode, inspection)

    service = PersonalArchiveTaskService(
        manager=task_manager,
        dependency_builder=build_personal_archive_dependencies,
        sync_factory=PersonalArchiveSync,
        render_func=render_personal_archive,
        inspector=inspect_archive_folder,
        legacy_stage_func=stage_legacy_archive,
        legacy_restore_func=restore_legacy_archive,
        legacy_finalize_func=finalize_legacy_archive,
        legacy_cleanup_func=cleanup_legacy_audit,
    )
    try:
        started = await service.start(
            req.model_copy(update={"output_dir": effective_dir}),
            {
                "uid": identity["uid"],
                "screen_name": identity["screen_name"],
                **(
                    {"login_uid": identity["login_uid"]}
                    if "login_uid" in identity
                    else {}
                ),
            },
        )
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    return PersonalArchiveStartResponse(
        task_id=started.task_id,
        mode=started.mode,
        self_uid=started.self_uid,
        self_screen_name=started.self_screen_name,
    )


async def _inspect_existing_archive(
    output_dir: str,
    target_uid: str | None,
) -> ArchiveFolderInspection:
    """解析目标身份并只读检查目录；非现有微博书目录返回 409。"""
    target = await asyncio.to_thread(_resolve_target_identity, target_uid)
    uid = target["uid"] if target is not None else _http_identity()["uid"]
    inspection = await asyncio.to_thread(
        inspect_archive_folder, output_dir, current_uid=uid
    )
    if inspection.state == "uid_mismatch":
        raise HTTPException(status_code=409, detail="该微博书属于其他账号，不允许访问")
    if inspection.state != "archive":
        raise HTTPException(status_code=409, detail="所选目录不是现有微博书目录")
    return inspection


def _archive_settings_from_inspection(
    inspection: ArchiveFolderInspection,
) -> ArchiveSettingsResponse:
    formats = inspection.output_formats
    return ArchiveSettingsResponse(
        state=inspection.state,
        output_formats={
            "pdf": bool(formats.get("pdf", True)),
            "markdown": bool(formats.get("markdown", True)),
        },
        history_complete=inspection.history_complete,
        history_cutoff=inspection.history_cutoff,
    )


@router.get("/api/backup/settings", response_model=ArchiveSettingsResponse)
async def api_backup_settings_get(
    output_dir: str = Query(..., description="微博书目录绝对路径"),
    target_uid: Optional[str] = Query(None, description="目标博主 UID，缺省为本人"),
) -> ArchiveSettingsResponse:
    target = await asyncio.to_thread(_resolve_target_identity, target_uid)
    uid = target["uid"] if target is not None else _http_identity()["uid"]
    inspection = await asyncio.to_thread(
        inspect_archive_folder, output_dir, current_uid=uid
    )
    return _archive_settings_from_inspection(inspection)


@router.post("/api/backup/settings", response_model=ArchiveSettingsResponse)
async def api_backup_settings_save(
    req: ArchiveSettingsUpdateRequest,
) -> ArchiveSettingsResponse:
    inspection = await _inspect_existing_archive(req.output_dir, req.target_uid)
    root = _physical_root(Path(req.output_dir))
    try:
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, inspection.uid)
            try:
                repository.write_output_formats(
                    pdf=req.output_formats.pdf,
                    markdown=req.output_formats.markdown,
                )
            finally:
                repository.close()
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    updated = await asyncio.to_thread(
        inspect_archive_folder, req.output_dir, current_uid=inspection.uid
    )
    return _archive_settings_from_inspection(updated)


def _render_preview(output_dir: str, uid: str, progress_callback=None) -> dict[str, object]:
    """锁内只生成互动 HTML 与离线数据，供「更新 HTML」后台任务调用。"""
    root = _physical_root(Path(output_dir))
    try:
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, uid)
            try:
                rendered = ArchiveRenderer(repository).render_all(
                    root,
                    cancel_requested=lambda: False,
                    pause_requested=lambda: False,
                    include_pdf=False,
                    include_markdown=False,
                    progress_callback=progress_callback,
                )
            finally:
                repository.close()
    except WeiboError:
        raise
    except Exception as exc:
        raise WeiboError("更新 HTML 失败", original=exc) from exc
    return {
        "generated_files": [
            path.relative_to(root).as_posix() for path in rendered.values()
        ],
        "output_dir": str(root),
    }


@router.post("/api/backup/preview", response_model=ArchivePreviewStartResponse)
async def api_backup_preview(req: ArchivePreviewRequest) -> ArchivePreviewStartResponse:
    inspection = await _inspect_existing_archive(req.output_dir, req.target_uid)
    task_id = await task_manager.create()
    started_at = time.monotonic()

    async def _emit(event: dict) -> None:
        normalized = dict(event)
        normalized["elapsed_seconds"] = time.monotonic() - started_at
        await task_manager.update_progress_event(task_id, normalized)

    async def _run() -> dict[str, object]:
        await _emit({
            "phase": "generate",
            "pct": 0.5,
            "detail": "正在更新互动 HTML",
            "current": 0,
            "total": 2,
            "unit": "file",
        })
        loop = asyncio.get_running_loop()
        def preview_progress(event):
            future = asyncio.run_coroutine_threadsafe(_emit(event), loop)
            future.result()
        try:
            result = await asyncio.to_thread(
                _render_preview, req.output_dir, inspection.uid, preview_progress
            )
        except WeiboError:
            raise
        except Exception as exc:
            raise WeiboError("更新 HTML 失败", original=exc) from exc
        generated = result["generated_files"]
        await _emit({
            "phase": "complete",
            "pct": 1.0,
            "detail": "HTML 已更新",
            "current": len(generated),
            "total": len(generated),
            "unit": "file",
        })
        return result

    asyncio.create_task(run_in_background(task_id, _run, manager=task_manager))
    return ArchivePreviewStartResponse(task_id=task_id)


@router.post("/api/following/start", response_model=FollowingArchiveStartResponse)
async def api_following_start(
    req: FollowingArchiveRequest,
) -> FollowingArchiveStartResponse:
    identity = _http_identity()
    service = FollowingArchiveTaskService(manager=task_manager)
    try:
        started = await service.start(
            req,
            {"uid": identity["uid"], "screen_name": identity["screen_name"]},
        )
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return FollowingArchiveStartResponse(
        task_id=started.task_id,
        self_uid=started.self_uid,
        self_screen_name=started.self_screen_name,
    )


@router.post(
    "/api/following/duration/check",
    response_model=FollowingDurationCheckResponse,
)
async def api_following_duration_check(
    req: FollowingDurationCheckRequest,
) -> FollowingDurationCheckResponse:
    inspection = await asyncio.to_thread(
        inspect_archive_folder,
        req.output_dir,
        current_uid=None,
    )
    if inspection.state != "archive" or not inspection.uid:
        raise HTTPException(status_code=409, detail="检查关注时长必须使用现有微博书目录")
    root = _physical_root(Path(req.output_dir))
    try:
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, inspection.uid)
            try:
                result = check_following_duration(
                    repository, req.object_type, req.object_id
                )
            finally:
                repository.close()
    except WeiboError as exc:
        status = 404 if exc.kind == WeiboErrorKind.NOT_FOUND else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return FollowingDurationCheckResponse(**asdict(result))


@router.post("/api/backup/archives", response_model=ArchiveLibraryResponse)
async def api_backup_archives(req: ArchiveInspectRequest) -> ArchiveLibraryResponse:
    """只读列出所选目录下的微博书档案（不限 UID），供微博书库面板展示。"""

    def _list() -> list[ArchiveFolderInspection]:
        direct = inspect_archive_folder(req.output_dir, current_uid=None)
        if direct.state == "archive":
            return [direct]
        return find_child_archives(req.output_dir, None, inspector=inspect_archive_folder)

    found = await asyncio.to_thread(_list)
    entries = [
        ArchiveLibraryEntry(
            path=item.path,
            uid=item.uid,
            screen_name=item.screen_name,
            total_posts=item.total_posts,
            last_successful_sync_at=item.last_successful_sync_at,
            history_complete=item.history_complete,
            history_cutoff=item.history_cutoff,
            book_format_version=item.book_format_version,
        )
        for item in found
    ]
    entries.sort(key=lambda item: item.last_successful_sync_at, reverse=True)
    return ArchiveLibraryResponse(entries=entries)


@router.post("/api/backup/video-quality/check", response_model=VideoQualityCheckResponse)
async def api_video_quality_check(req: VideoQualityCheckRequest) -> VideoQualityCheckResponse:
    from backend.app.services.video_quality_checks import start_check
    target = await asyncio.to_thread(_resolve_target_identity, req.target_uid)
    identity = target if target is not None else _http_identity()
    inspection = await asyncio.to_thread(inspect_archive_folder, req.output_dir, current_uid=identity["uid"])
    _ensure_mode_allowed("incremental", inspection)
    try:
        result = await start_check(
            req.output_dir, identity["uid"], identity.get("login_uid", identity["uid"]),
            manager=task_manager, dependency_builder=build_personal_archive_dependencies,
            pacing_mode=req.pacing_mode,
        )
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return VideoQualityCheckResponse(**result)


@router.get("/api/backup/library", response_model=ArchiveLibraryResponse)
async def api_library_list() -> ArchiveLibraryResponse:
    from backend.app.services.archive_library import list_archives
    try:
        return ArchiveLibraryResponse(entries=await asyncio.to_thread(list_archives))
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


from backend.app.schemas import ArchiveLibraryRegistration, ArchiveLibraryMutation, ArchiveSizeResponse, ArchiveSizeRow


@router.get("/api/backup/library/sizes", response_model=ArchiveSizeResponse)
async def api_library_sizes(path: str | None = None) -> ArchiveSizeResponse:
    from backend.app.services.archive_library import archive_size_bytes, registered_archive_sizes
    try:
        if path is not None:
            size = await asyncio.to_thread(archive_size_bytes, path)
            return ArchiveSizeResponse(sizes=[ArchiveSizeRow(path=path, size_bytes=size)])
        rows = await asyncio.to_thread(registered_archive_sizes)
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ArchiveSizeResponse(sizes=[ArchiveSizeRow(**row) for row in rows])


@router.post("/api/backup/library/register", response_model=ArchiveLibraryResponse)
async def api_library_register(req: ArchiveLibraryRegistration) -> ArchiveLibraryResponse:
    from backend.app.services.archive_library import register_archive
    try:
        await asyncio.to_thread(register_archive, req.output_dir, req.uid)
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await api_library_list()


@router.post("/api/backup/library/relocate", response_model=ArchiveLibraryResponse)
async def api_library_relocate(req: ArchiveLibraryMutation) -> ArchiveLibraryResponse:
    from backend.app.services.archive_library import relocate_archive
    if not req.output_dir:
        raise HTTPException(status_code=400, detail="请选择微博书的新目录")
    try:
        await asyncio.to_thread(relocate_archive, req.path, req.uid, req.output_dir)
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await api_library_list()


@router.post("/api/backup/library/remove", response_model=ArchiveLibraryResponse)
async def api_library_remove(req: ArchiveLibraryMutation) -> ArchiveLibraryResponse:
    from backend.app.services.archive_library import remove_archive
    try:
        await asyncio.to_thread(remove_archive, req.path, req.uid)
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await api_library_list()
