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
    BackupIndex,
    FollowingArchiveRequest,
    FollowingArchiveStartResponse,
    FollowingDurationCheckRequest,
    FollowingDurationCheckResponse,
    PersonalArchiveRequest,
    PersonalArchiveStartResponse,
    WhoamiResponse,
)
from backend.app.services.archive_folder import (
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
            rendered = ArchiveRenderer(repository).render_all(
                root,
                cancel_requested=cancel_requested,
                pause_requested=pause_requested,
                begin_commit=begin_commit,
            )
        finally:
            repository.close()
    return [
        rendered[key].relative_to(root).as_posix()
        for key in ("html", "pdf", "markdown", "data")
    ]


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


def _resolve_target_identity(req_target_uid: str | None) -> dict[str, str] | None:
    """``target_uid`` 提供时返回目标博主身份；等于登录账号或缺省时返回 None（本人模式）。"""
    if req_target_uid is None:
        return None
    target_uid = req_target_uid.strip()
    if not _UID_PATTERN.fullmatch(target_uid):
        raise HTTPException(status_code=400, detail="目标博主 UID 无效")
    login_uid = _http_identity()["uid"]
    if target_uid == login_uid:
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
    if not info.uid or not info.screen_name:
        raise HTTPException(status_code=400, detail="目标博主信息无效")
    return {"uid": str(info.uid), "screen_name": info.screen_name, "login_uid": login_uid}


class _ProgressReporter:
    """从同步工作线程向 TaskManager 发送结构化进度。"""

    def __init__(self, task_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.task_id = task_id
        self.loop = loop
        self.started_at = time.monotonic()
        self._pending: list[Any] = []
        self._last_pct = 0.0

    def emit(self, event: dict) -> None:
        normalized = dict(event)
        if normalized.get("phase") == "complete":
            normalized.update({
                "phase": "generate",
                "pct": 0.96,
                "detail": "归档数据已完成，正在生成三种格式与离线数据",
                "current": 0,
                "total": 4,
                "unit": "file",
            })
        self._queue(normalized)

    def emit_render_complete(self) -> None:
        self._queue({
            "phase": "complete",
            "pct": 1.0,
            "detail": "微博书归档与固定文件已完成",
            "current": 4,
            "total": 4,
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
    if mode in {"incremental", "rebuild"} and inspection.state != "archive":
        raise HTTPException(status_code=409, detail="增量同步或重建必须使用现有微博书目录")


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
