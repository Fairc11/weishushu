"""本人微博书持久任务的唯一协调入口。"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.app.schemas import ArchiveFolderInspection, PersonalArchiveRequest
from backend.app.services.archive_folder import (
    inspect_archive_folder,
    resolve_archive_dir,
)
from backend.app.services.backup_index import (
    cleanup_legacy_audit,
    finalize_legacy_archive,
    legacy_finalize_completed,
    legacy_stage_path,
    restore_legacy_archive,
    stage_legacy_archive,
    staged_legacy_archive_exists,
    staged_legacy_archive_sha256,
    staged_legacy_archive_uid,
)
from backend.app.services.task_manager import TaskManager, run_in_background, task_manager
from backend.app.services.system_power import KeepAwakeLease, system_power_service
from weibo_book import WeiboBook
from weibo_book.archive.render_snapshot import ArchiveRenderer
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.pacing import AdaptiveRequestScheduler, PacingStatus
from weibo_book.archive.source import ArchiveMediaStager, WeiboArchiveSource
from weibo_book.archive.sync import (
    PersonalArchiveSync,
    _archive_lock,
    _physical_root,
    _rebuild_state_path,
)
from weibo_book.errors import OperationCancelled, OperationPaused, WeiboError, WeiboErrorKind
from weibo_book.extractor import WeiboExtractor


@dataclass(frozen=True)
class TaskStartInfo:
    task_id: str
    mode: str
    self_uid: str
    self_screen_name: str
    worker: asyncio.Task


class _FixedIdentityProvider:
    def __init__(self, identity: dict[str, str]) -> None:
        self._identity = dict(identity)

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
    cancel_requested: Callable[[], bool],
    begin_commit: Callable[[], bool] | None = None,
    pause_requested: Callable[[], bool] | None = None,
) -> list[str]:
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


class PersonalArchiveTaskService:
    def __init__(
        self,
        *,
        manager: TaskManager = task_manager,
        dependency_builder: Callable[[str], tuple[Any, Any]] = build_personal_archive_dependencies,
        sync_factory: Callable[..., Any] = PersonalArchiveSync,
        render_func: Callable[..., list[str]] = render_personal_archive,
        inspector: Callable[..., ArchiveFolderInspection] = inspect_archive_folder,
        legacy_stage_func: Callable[[str, str, str], Path] = stage_legacy_archive,
        legacy_restore_func: Callable[[str, Path, str, str], None] = restore_legacy_archive,
        legacy_finalize_func: Callable[[str, Path, str, str, str, str, str], None] = finalize_legacy_archive,
        legacy_cleanup_func: Callable[[str], None] = cleanup_legacy_audit,
    ) -> None:
        self.manager = manager
        self.dependency_builder = dependency_builder
        self.sync_factory = sync_factory
        self.render_func = render_func
        self.inspector = inspector
        self.legacy_stage_func = legacy_stage_func
        self.legacy_restore_func = legacy_restore_func
        self.legacy_finalize_func = legacy_finalize_func
        self.legacy_cleanup_func = legacy_cleanup_func

    @staticmethod
    def _identity(identity: dict[str, str]) -> tuple[str, str]:
        uid = identity.get("uid") if isinstance(identity, dict) else None
        screen_name = identity.get("screen_name") if isinstance(identity, dict) else None
        if not isinstance(uid, str) or not uid.strip() or not isinstance(screen_name, str) or not screen_name.strip():
            raise WeiboError("当前登录账号信息无效", kind=WeiboErrorKind.AUTH)
        return uid, screen_name

    def _archive_target_identity(
        self,
        output_dir: str,
        expected_uid: str,
        *,
        mode: str | None = None,
        task_id: str | None = None,
    ) -> tuple[str, str]:
        """他人归档任务：从本地档案 manifest 还原目标账号身份。

        归档属于目标 UID，任何有效登录都可以续跑公开数据抓取；
        身份以本地档案记录为准，不以当前登录账号为准。
        create/rebuild 模式在提交前正式目录尚不存在，数据在
        「.名称.模式-task-任务ID」临时归档中，身份确认要回退到那里。
        """
        inspection = self.inspector(output_dir, current_uid=expected_uid)
        if inspection.uid == expected_uid and inspection.screen_name:
            return expected_uid, inspection.screen_name
        if mode in {"create", "rebuild"} and task_id:
            root = _physical_root(Path(output_dir))
            temporary = root.parent / f".{root.name}.{mode}-task-{task_id}"
            if temporary.is_dir() and not temporary.is_symlink():
                temp_inspection = self.inspector(str(temporary), current_uid=expected_uid)
                if temp_inspection.uid == expected_uid and temp_inspection.screen_name:
                    return expected_uid, temp_inspection.screen_name
            if (
                mode == "create"
                and inspection.state == "empty"
                and not temporary.exists()
                and not temporary.is_symlink()
            ):
                # 旧版本崩溃路径已删除临时归档：本地进度彻底丢失，
                # 唯一出路是放弃僵尸记录后重新开始。
                raise WeiboError(
                    "上次中断的本地数据已被旧版本清理，无法继续。请放弃该任务后重新开始备份",
                    kind=WeiboErrorKind.AUTH,
                )
        raise WeiboError("无法确认归档目标账号，请检查微博书目录", kind=WeiboErrorKind.AUTH)

    @staticmethod
    def _login_uid(identity: dict[str, str], fallback: str) -> str:
        """当前登录账号 UID；缺省等于归档目标（本人模式）。"""
        login_uid = identity.get("login_uid") if isinstance(identity, dict) else None
        if not isinstance(login_uid, str) or not login_uid.strip():
            return fallback
        return login_uid

    @staticmethod
    def _ensure_mode_allowed(mode: str, inspection: ArchiveFolderInspection) -> None:
        if inspection.state == "uid_mismatch":
            raise WeiboError("该微博书属于其他账号，不允许覆盖", kind=WeiboErrorKind.AUTH)
        if inspection.state == "ordinary_nonempty":
            raise WeiboError("所选路径是非空目录，请选择空目录", kind=WeiboErrorKind.API)
        if inspection.state == "damaged":
            raise WeiboError("所选微博书已损坏，请先处理目录问题", kind=WeiboErrorKind.PARSE)
        if mode == "create" and inspection.state not in {"empty", "legacy_index"}:
            raise WeiboError("首次建立微博书只能使用空目录", kind=WeiboErrorKind.API)
        if mode in {"incremental", "rebuild"} and inspection.state != "archive":
            raise WeiboError("增量同步或重建必须使用现有微博书目录", kind=WeiboErrorKind.API)

    def _resolve_archive_dir(
        self,
        selected: str,
        uid: str,
        screen_name: str,
    ) -> str:
        """把用户所选目录解析为实际微博书根目录（只读，不创建任何内容）。

        解析规则见 ``archive_folder.resolve_archive_dir``；模式合法性仍由
        ``_ensure_mode_allowed`` 对解析后的目录检查结果把关。
        """
        return resolve_archive_dir(
            selected,
            uid,
            screen_name,
            inspector=self.inspector,
        )

    async def start(
        self,
        request: PersonalArchiveRequest,
        identity: dict[str, str],
    ) -> TaskStartInfo:
        uid, screen_name = self._identity(identity)
        login_uid = self._login_uid(identity, uid)
        effective_dir = await asyncio.to_thread(
            self._resolve_archive_dir,
            request.output_dir,
            uid,
            screen_name,
        )
        inspection = await asyncio.to_thread(
            self.inspector,
            effective_dir,
            current_uid=uid,
        )
        self._ensure_mode_allowed(request.mode, inspection)
        task_id = await self.manager.create_personal_archive(
            mode=request.mode,
            output_dir=effective_dir,
            expected_uid=uid,
            pacing_mode=request.pacing_mode,
            keep_awake_when_plugged=request.keep_awake_when_plugged,
            target_label=screen_name if login_uid != uid else None,
        )
        return self._launch(
            task_id, request.mode, effective_dir, uid, screen_name,
            resuming=False, login_uid=login_uid,
        )

    async def resume(
        self,
        task_id: str,
        identity: dict[str, str],
    ) -> TaskStartInfo:
        uid, screen_name = self._identity(identity)
        login_uid = self._login_uid(identity, uid)
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            raise WeiboError("未找到可恢复的归档任务", kind=WeiboErrorKind.API)
        persistent = record.persistent_record
        if persistent.expected_uid is not None and persistent.expected_uid != uid:
            # 他人归档：以本地档案记录的目标账号为准，任何有效登录均可续跑
            uid, screen_name = await asyncio.to_thread(
                self._archive_target_identity,
                persistent.output_dir,
                persistent.expected_uid,
                mode=persistent.mode,
                task_id=task_id,
            )
        legacy_uid = await asyncio.to_thread(
            self._staged_legacy_uid_if_present,
            persistent.output_dir,
            task_id,
        )
        if legacy_uid is not None and legacy_uid != uid:
            raise WeiboError("当前账号与旧版备份账号不一致", kind=WeiboErrorKind.AUTH)
        if persistent.expected_uid is None:
            local_uid = await asyncio.to_thread(
                self._local_resume_uid,
                persistent,
            )
            if local_uid != uid:
                # 他人归档：以本地档案记录的目标账号为准
                uid, screen_name = await asyncio.to_thread(
                    self._archive_target_identity,
                    persistent.output_dir,
                    local_uid,
                    mode=persistent.mode,
                    task_id=task_id,
                )
            await self.manager.set_expected_uid(task_id, uid)
            persistent = record.persistent_record
        persistent = await self._ensure_legacy_index_sha256(task_id, persistent)
        await asyncio.to_thread(
            self._validate_render_phase_legacy_completion,
            persistent,
            uid,
        )
        inspection = await asyncio.to_thread(
            self.inspector,
            persistent.output_dir,
            current_uid=uid,
        )
        if inspection.state == "uid_mismatch":
            raise WeiboError("当前账号与归档账号不一致", kind=WeiboErrorKind.AUTH)
        if persistent.phase == "render" and inspection.state != "archive":
            raise WeiboError("已同步的微博书归档不可用", kind=WeiboErrorKind.PARSE)
        if not await self.manager.prepare_persistent_resume(task_id):
            raise WeiboError("当前任务状态不允许恢复", kind=WeiboErrorKind.API)
        return self._launch(
            task_id,
            persistent.mode,
            persistent.output_dir,
            uid,
            screen_name,
            resuming=True,
            login_uid=login_uid,
        )

    def _launch(
        self,
        task_id: str,
        mode: str,
        output_dir: str,
        uid: str,
        screen_name: str,
        *,
        resuming: bool,
        login_uid: str | None = None,
    ) -> TaskStartInfo:
        worker = asyncio.create_task(
            run_in_background(
                task_id,
                lambda: self._execute(
                    task_id, mode, output_dir, uid, screen_name,
                    resuming=resuming, login_uid=login_uid,
                ),
                manager=self.manager,
                persistent_cancel_handler=self.finish_accepted_cancel,
            )
        )
        record = self.manager.get(task_id)
        if record is not None:
            record._asyncio_task = worker
        return TaskStartInfo(task_id, mode, uid, screen_name, worker)

    async def _execute(
        self,
        task_id: str,
        mode: str,
        output_dir: str,
        uid: str,
        screen_name: str,
        *,
        resuming: bool,
        login_uid: str | None = None,
    ) -> dict[str, object]:
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        loop = asyncio.get_running_loop()
        keep_awake = KeepAwakeLease(
            system_power_service,
            enabled=(
                record.persistent_record.keep_awake_when_plugged
                and record.persistent_record.pacing_mode != "standard"
            ),
            reason="低强度本人微博书归档",
        )
        keep_awake.refresh()
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.add_done_callback(lambda _task: keep_awake.close())

        execute_started = time.monotonic()
        last_pct = 0.0

        def progress(event: dict) -> None:
            nonlocal last_pct
            normalized = dict(event)
            # sync 的 complete 只代表抓取结束，固定文件尚未渲染；
            # 改写为 generate 阶段，避免前端提前展示「已完成」。
            if normalized.get("phase") == "complete":
                normalized.update({
                    "phase": "generate",
                    "pct": 0.96,
                    "detail": "归档数据已完成，正在生成三种格式与离线数据",
                    "current": 0,
                    "total": 4,
                    "unit": "file",
                })
            last_pct = max(last_pct, float(normalized.get("pct") or 0.0))
            normalized["pct"] = last_pct
            normalized["elapsed_seconds"] = time.monotonic() - execute_started
            future = asyncio.run_coroutine_threadsafe(
                self.manager.update_progress_event(task_id, normalized),
                loop,
            )
            future.result()

        def save_run_id(run_id: str) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self.manager.set_archive_run_id(task_id, run_id),
                loop,
            )
            future.result()

        def pacing_status(status: PacingStatus) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self.manager.update_pacing_status(task_id, status),
                loop,
            )
            future.result()

        result: dict[str, object] = {
            "mode": mode,
            "new_posts": 0,
            "refreshed_posts": 0,
            "changed_posts": 0,
            "unavailable_posts": 0,
        }
        initial_inspection = await asyncio.to_thread(
            self.inspector,
            output_dir,
            current_uid=uid,
        )
        if (
            resuming
            and record.persistent_record.phase == "sync"
            and mode in {"create", "rebuild"}
            and initial_inspection.state == "archive"
            and initial_inspection.uid == uid
            and await asyncio.to_thread(
                self._replacement_sync_is_already_complete,
                output_dir,
                uid,
                mode,
                task_id,
            )
        ):
            await self.manager.set_persistent_phase(task_id, "render")
        legacy_stage: Path | None = None
        exact_stage = legacy_stage_path(output_dir, task_id)
        if staged_legacy_archive_exists(output_dir, task_id):
            legacy_uid = await asyncio.to_thread(
                staged_legacy_archive_uid,
                output_dir,
                task_id,
            )
            if record.persistent_record.expected_uid is None or legacy_uid != record.persistent_record.expected_uid:
                raise WeiboError(
                    "旧版备份账号与持久任务账号不一致",
                    kind=WeiboErrorKind.AUTH,
                )
            legacy_stage = exact_stage
        elif record.persistent_record.phase == "sync" and initial_inspection.state == "legacy_index":
            legacy_stage = await asyncio.to_thread(
                self.legacy_stage_func,
                output_dir,
                record.persistent_record.expected_uid,
                task_id,
            )
        persistent = await self._ensure_legacy_index_sha256(
            task_id,
            record.persistent_record,
        )
        legacy_context = await asyncio.to_thread(
            self._validate_render_phase_legacy_completion,
            persistent,
            persistent.expected_uid,
        )
        if legacy_context and legacy_stage is None:
            legacy_stage = exact_stage
        try:
            if record.persistent_record.phase == "sync":
                scheduler = AdaptiveRequestScheduler(
                    record.persistent_record.pacing_mode,
                    pause_event=record._pause_event,
                    cancel_event=record._cancel_event,
                    status_callback=pacing_status,
                )
                def power_snapshot():
                    snapshot = system_power_service.snapshot()
                    keep_awake.refresh(snapshot)
                    return snapshot

                scheduler.set_power_snapshot_provider(power_snapshot)
                try:
                    builder_parameters = inspect.signature(
                        self.dependency_builder
                    ).parameters
                    builder_kwargs: dict[str, object] = {}
                    if "pacing_scheduler" in builder_parameters:
                        builder_kwargs["pacing_scheduler"] = scheduler
                    if "login_uid" in builder_parameters:
                        builder_kwargs["login_uid"] = login_uid or uid
                    source, media_stager = self.dependency_builder(
                        uid, **builder_kwargs
                    )
                    wake_probe = getattr(source, "probe_session", None)
                    if callable(wake_probe):
                        scheduler.set_wake_probe(wake_probe)
                    sync = self.sync_factory(
                        output_dir,
                        source,
                        _FixedIdentityProvider({"uid": uid, "screen_name": screen_name}),
                        media_stager=media_stager,
                        cancel_requested=record._cancel_event.is_set,
                        pause_requested=record._pause_event.is_set,
                        progress_callback=progress,
                        task_id=task_id,
                        sync_run_started=save_run_id,
                        pacing_scheduler=scheduler,
                    )
                    sync_result = await asyncio.to_thread(sync.run, mode)
                finally:
                    scheduler.close()
                result.update(asdict(sync_result))
                await self.manager.set_persistent_phase(task_id, "render")

            keep_awake.start_monitoring()
            # 渲染开始前明确进入 generate 阶段；从 render 阶段恢复时
            # sync 不会再跑，progress() 不会有任何事件。
            last_pct = max(last_pct, 0.96)
            await self.manager.update_progress_event(task_id, {
                "phase": "generate",
                "pct": last_pct,
                "detail": "归档数据已完成，正在生成三种格式与离线数据",
                "current": 0,
                "total": 4,
                "unit": "file",
                "elapsed_seconds": time.monotonic() - execute_started,
            })
            generated_files = await asyncio.to_thread(
                self.render_func,
                output_dir,
                uid,
                record._cancel_event.is_set,
                record.try_begin_commit,
                record._pause_event.is_set,
            )
        except BaseException:
            try:
                if (
                    legacy_stage is not None
                    and record.persistent_record.phase == "sync"
                ):
                    await asyncio.to_thread(
                        self.legacy_restore_func,
                        output_dir,
                        legacy_stage,
                        task_id,
                        record.persistent_record.expected_uid,
                    )
            finally:
                if keep_awake is not None:
                    keep_awake.close()
            raise
        try:
            if legacy_stage is not None:
                await asyncio.to_thread(
                    self._validate_render_phase_legacy_completion,
                    record.persistent_record,
                    record.persistent_record.expected_uid,
                )
                await asyncio.to_thread(
                    self.legacy_finalize_func,
                    output_dir,
                    legacy_stage,
                    task_id,
                    record.persistent_record.expected_uid,
                    record.persistent_record.archive_run_id,
                    record.persistent_record.mode,
                    record.persistent_record.legacy_index_sha256,
                )
            else:
                await asyncio.to_thread(self.legacy_cleanup_func, output_dir)
            inspection = await asyncio.to_thread(
                self.inspector,
                output_dir,
                current_uid=uid,
            )
            if inspection.state != "archive" or inspection.uid != uid:
                raise WeiboError("完成后的微博书归档验证失败", kind=WeiboErrorKind.PARSE)
            result["generated_files"] = generated_files
            result["total_posts"] = inspection.total_posts
            # 三种格式与离线数据全部就绪后才允许前端展示「已完成」。
            await self.manager.update_progress_event(task_id, {
                "phase": "complete",
                "pct": 1.0,
                "detail": "微博书归档与固定文件已完成",
                "current": 4,
                "total": 4,
                "unit": "file",
                "elapsed_seconds": time.monotonic() - execute_started,
            })
            return result
        finally:
            if keep_awake is not None:
                keep_awake.close()

    @staticmethod
    def _replacement_sync_is_already_complete(
        output_dir: str,
        uid: str,
        mode: str,
        task_id: str,
    ) -> bool:
        root = _physical_root(Path(output_dir))
        temporary = root.parent / f".{root.name}.{mode}-task-{task_id}"
        if temporary.is_symlink():
            raise WeiboError("任务临时归档不能是符号链接", kind=WeiboErrorKind.API)
        if temporary.exists():
            return False
        repository = ArchiveRepository.open(root, uid)
        try:
            latest = repository.get_latest_sync_status(mode)
            return (
                latest is not None
                and latest[1] == "done"
                and bool(repository.manifest().last_successful_sync_at)
            )
        finally:
            repository.close()

    async def pause(self, task_id: str) -> bool:
        return await self.manager.request_pause(task_id)

    async def cancel(self, task_id: str) -> bool:
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            return await self.manager.request_cancel(task_id)
        if record.state not in {"waiting_resume", "error"}:
            return await self.manager.request_cancel(task_id)
        persistent = record.persistent_record
        expected_uid = persistent.expected_uid
        if expected_uid is None:
            raise WeiboError("旧版持久任务缺少可信账号标识，请先继续任务确认账号", kind=WeiboErrorKind.AUTH)
        persistent = await self._ensure_legacy_index_sha256(task_id, persistent)
        if await asyncio.to_thread(
            self._finalize_render_phase_legacy_if_present,
            persistent,
            expected_uid,
        ):
            await self.manager.set_cancelled(task_id)
            return True
        if persistent.archive_run_id is None:
            legacy_uid = await asyncio.to_thread(
                self._staged_legacy_uid_if_present,
                persistent.output_dir,
                task_id,
            )
            if legacy_uid is None:
                raise WeiboError("持久任务缺少精确同步记录标识", kind=WeiboErrorKind.PARSE)
            if legacy_uid != expected_uid:
                raise WeiboError("旧版备份账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
            await asyncio.to_thread(
                self._restore_staged_legacy,
                persistent.output_dir,
                task_id,
                expected_uid,
            )
            await self.manager.set_cancelled(task_id)
            return True
        uid = await asyncio.to_thread(
            self._local_archive_uid,
            persistent.output_dir,
            persistent.mode,
            task_id,
        )
        if uid != expected_uid:
            raise WeiboError("本地归档账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
        await asyncio.to_thread(
            self._cleanup_local_state,
            persistent.output_dir,
            uid,
            persistent.mode,
            task_id,
            persistent.archive_run_id,
            "cancelled",
        )
        await asyncio.to_thread(
            self._restore_staged_legacy_if_present,
            persistent.output_dir,
            task_id,
            uid,
        )
        await self.manager.set_cancelled(task_id)
        return True

    async def abandon(self, task_id: str, identity: dict[str, str]) -> bool:
        uid, _screen_name = self._identity(identity)
        record = self.manager.get(task_id)
        if (
            record is None
            or record.persistent_record is None
            or record.state not in {"waiting_resume", "error"}
        ):
            return False
        worker = record._asyncio_task
        if (
            worker is not None
            and worker is not asyncio.current_task()
            and not worker.done()
        ):
            await asyncio.shield(worker)
        persistent = record.persistent_record
        # 他人归档：放弃清理只操作本地文件，以持久任务记录的目标账号为准，
        # 不要求当前登录账号与之一致。
        if persistent.expected_uid is None:
            local_uid = await asyncio.to_thread(
                self._local_resume_uid,
                persistent,
            )
            await self.manager.set_expected_uid(task_id, local_uid)
            persistent = record.persistent_record
        expected_uid = persistent.expected_uid
        if expected_uid is None:
            raise WeiboError("持久任务缺少可信账号标识", kind=WeiboErrorKind.AUTH)
        persistent = await self._ensure_legacy_index_sha256(task_id, persistent)
        legacy_uid = await asyncio.to_thread(
            self._staged_legacy_uid_if_present,
            persistent.output_dir,
            task_id,
        )
        if legacy_uid is not None and legacy_uid != expected_uid:
            raise WeiboError("当前账号与待放弃归档账号不一致", kind=WeiboErrorKind.AUTH)
        if await asyncio.to_thread(
            self._finalize_render_phase_legacy_if_present,
            persistent,
            expected_uid,
        ):
            await self.manager.set_abandoned(task_id)
            return True
        local_uid = legacy_uid if legacy_uid is not None else expected_uid
        if persistent.archive_run_id is None:
            if legacy_uid is None:
                inspection = await asyncio.to_thread(
                    self.inspector,
                    persistent.output_dir,
                    current_uid=expected_uid,
                )
                untouched_create = (
                    persistent.phase == "sync"
                    and persistent.mode == "create"
                    and inspection.state == "empty"
                )
                untouched_existing = (
                    persistent.phase == "sync"
                    and persistent.mode in {"incremental", "rebuild"}
                    and inspection.state == "archive"
                    and inspection.uid == expected_uid
                )
                if untouched_create or untouched_existing:
                    await self.manager.set_abandoned(task_id)
                    return True
                raise WeiboError("持久任务缺少精确同步记录标识", kind=WeiboErrorKind.PARSE)
            await asyncio.to_thread(
                self._restore_staged_legacy,
                persistent.output_dir,
                task_id,
                local_uid,
            )
            await self.manager.set_abandoned(task_id)
            return True
        await asyncio.to_thread(
            self._cleanup_local_state,
            persistent.output_dir,
            local_uid,
            persistent.mode,
            task_id,
            persistent.archive_run_id,
            "abandoned",
        )
        await asyncio.to_thread(
            self._restore_staged_legacy_if_present,
            persistent.output_dir,
            task_id,
            local_uid,
        )
        await self.manager.set_abandoned(task_id)
        return True

    async def finish_interrupted_cancel(self, persistent) -> None:
        if persistent.state != "cancelling":
            return
        expected_uid = persistent.expected_uid
        if expected_uid is None:
            failed = replace(
                persistent,
                state="error",
                saved_at=datetime.now(timezone.utc).isoformat(),
                pause_reason="取消清理缺少可信账号标识，请登录后继续处理",
            )
            self.manager._persistent_store.save(failed)
            await self.manager.restore_waiting_record(failed)
            return
        try:
            persistent = await self._ensure_legacy_index_sha256(
                persistent.task_id,
                persistent,
            )
            if await asyncio.to_thread(
                self._finalize_render_phase_legacy_if_present,
                persistent,
                expected_uid,
            ):
                self.manager._persistent_store.clear()
                return
        except WeiboError:
            failed = replace(
                persistent,
                state="error",
                saved_at=datetime.now(timezone.utc).isoformat(),
                pause_reason="取消清理未完成，请查看日志",
            )
            self.manager._persistent_store.save(failed)
            await self.manager.restore_waiting_record(failed)
            return
        if persistent.archive_run_id is None:
            try:
                uid = await asyncio.to_thread(
                    self._staged_legacy_uid_if_present,
                    persistent.output_dir,
                    persistent.task_id,
                )
                if uid is not None:
                    if uid != expected_uid:
                        raise WeiboError(
                            "旧版备份账号与持久任务账号不一致",
                            kind=WeiboErrorKind.AUTH,
                        )
                    await asyncio.to_thread(
                        self._restore_staged_legacy,
                        persistent.output_dir,
                        persistent.task_id,
                        expected_uid,
                    )
                    self.manager._persistent_store.clear()
                    return
            except WeiboError:
                pass
            failed = replace(
                persistent,
                state="error",
                saved_at=datetime.now(timezone.utc).isoformat(),
                pause_reason="取消清理缺少精确同步记录标识",
            )
            self.manager._persistent_store.save(failed)
            await self.manager.restore_waiting_record(failed)
            return

        try:
            uid = await asyncio.to_thread(
                self._local_archive_uid,
                persistent.output_dir,
                persistent.mode,
                persistent.task_id,
            )
            if uid != expected_uid:
                raise WeiboError(
                    "本地归档账号与持久任务账号不一致",
                    kind=WeiboErrorKind.AUTH,
                )
            await asyncio.to_thread(
                self._cleanup_local_state,
                persistent.output_dir,
                uid,
                persistent.mode,
                persistent.task_id,
                persistent.archive_run_id,
                "cancelled",
            )
            await asyncio.to_thread(
                self._restore_staged_legacy_if_present,
                persistent.output_dir,
                persistent.task_id,
                expected_uid,
            )
            self.manager._persistent_store.clear()
        except WeiboError:
            failed = replace(
                persistent,
                state="error",
                saved_at=datetime.now(timezone.utc).isoformat(),
                pause_reason="取消清理未完成，请查看日志",
            )
            self.manager._persistent_store.save(failed)
            await self.manager.restore_waiting_record(failed)

    async def finish_accepted_cancel(self, task_id: str) -> None:
        """工作器在取消已接受后失败时，复用启动期本地安全收尾。"""
        record = self.manager.get(task_id)
        if (
            record is None
            or record.persistent_record is None
            or record.state != "cancelling"
        ):
            return
        await self.finish_interrupted_cancel(record.persistent_record)
        retained = self.manager._persistent_store.load()
        if retained is None:
            await self.manager.set_cancelled(task_id)
            return
        if retained.task_id == task_id and retained.state == "error":
            record.persistent_record = retained
            await self.manager.set_error(
                task_id,
                retained.pause_reason,
                error_recoverable=retained.error_recoverable,
            )

    @classmethod
    def _cleanup_local_state(
        cls,
        output_dir: str,
        uid: str,
        mode: str,
        task_id: str,
        run_id: str,
        terminal_status: str,
    ) -> None:
        root = _physical_root(Path(output_dir))
        if _rebuild_state_path(root).exists():
            raise WeiboError("存在活动目录替换日志，拒绝放弃清理", kind=WeiboErrorKind.API)
        render_journal = root / "data" / ".weishushu-render-state.json"
        if render_journal.exists() or render_journal.is_symlink():
            raise WeiboError("存在活动固定文件发布日志，拒绝放弃清理", kind=WeiboErrorKind.API)
        if mode in {"create", "rebuild"}:
            temporary = root.parent / f".{root.name}.{mode}-task-{task_id}"
            if not temporary.exists() and not temporary.is_symlink():
                # 旧版本崩溃路径会删除临时归档（僵尸记录）：本地已无进度
                # 可清理，同步恢复点随临时归档一并消失，直接放行放弃。
                return
            cls._validate_task_tree(temporary, root.parent)
            repository = ArchiveRepository.open(temporary, uid)
            try:
                sync = repository.get_sync_run(run_id)
                if sync.mode != mode:
                    raise WeiboError("同步记录模式与持久任务不一致", kind=WeiboErrorKind.PARSE)
                repository.clear_sync_checkpoint(run_id, terminal_status)
            finally:
                repository.close()
            shutil.rmtree(temporary)
            cls._fsync_parent(temporary.parent)
            return

        repository = ArchiveRepository.open(root, uid)
        try:
            sync = repository.get_sync_run(run_id)
            if sync.mode != "incremental":
                raise WeiboError("同步记录模式与持久任务不一致", kind=WeiboErrorKind.PARSE)
            repository.clear_sync_checkpoint(run_id, terminal_status)
        finally:
            repository.close()
        work = root / ".work" / run_id
        if work.exists() or work.is_symlink():
            cls._validate_task_tree(work, root / ".work")
            shutil.rmtree(work)
            cls._fsync_parent(work.parent)

    def _local_archive_uid(
        self,
        output_dir: str,
        mode: str,
        task_id: str,
    ) -> str:
        legacy_uid = self._staged_legacy_uid_if_present(output_dir, task_id)
        if legacy_uid is not None:
            return legacy_uid
        root = _physical_root(Path(output_dir))
        target = (
            root.parent / f".{root.name}.{mode}-task-{task_id}"
            if mode in {"create", "rebuild"}
            else root
        )
        inspection = self.inspector(str(target), current_uid="")
        if inspection.state != "uid_mismatch" or not inspection.uid:
            raise WeiboError("无法确认待取消归档的账号标识", kind=WeiboErrorKind.PARSE)
        return inspection.uid

    @staticmethod
    def _staged_legacy_uid_if_present(
        output_dir: str,
        task_id: str,
    ) -> str | None:
        staged = legacy_stage_path(output_dir, task_id)
        if not staged_legacy_archive_exists(output_dir, task_id):
            return None
        return staged_legacy_archive_uid(output_dir, task_id)

    def _restore_staged_legacy(
        self,
        output_dir: str,
        task_id: str,
        expected_uid: str,
    ) -> None:
        staged = legacy_stage_path(output_dir, task_id)
        self.legacy_restore_func(
            output_dir,
            staged,
            task_id,
            expected_uid,
        )

    def _restore_staged_legacy_if_present(
        self,
        output_dir: str,
        task_id: str,
        expected_uid: str,
    ) -> None:
        staged = legacy_stage_path(output_dir, task_id)
        if not staged_legacy_archive_exists(output_dir, task_id):
            return
        self.legacy_restore_func(
            output_dir,
            staged,
            task_id,
            expected_uid,
        )

    def _finalize_render_phase_legacy_if_present(
        self,
        persistent,
        expected_uid: str,
    ) -> bool:
        if not self._validate_render_phase_legacy_completion(
            persistent,
            expected_uid,
        ):
            return False
        staged = legacy_stage_path(persistent.output_dir, persistent.task_id)
        self.legacy_finalize_func(
            persistent.output_dir,
            staged,
            persistent.task_id,
            expected_uid,
            persistent.archive_run_id,
            persistent.mode,
            persistent.legacy_index_sha256,
        )
        return True

    def _validate_render_phase_legacy_completion(
        self,
        persistent,
        expected_uid: str | None,
    ) -> bool:
        if persistent.phase != "render":
            return False
        if persistent.mode != "create":
            return False
        if expected_uid is None:
            raise WeiboError("持久任务缺少可信账号标识", kind=WeiboErrorKind.AUTH)
        stage_exists = staged_legacy_archive_exists(
            persistent.output_dir,
            persistent.task_id,
        )
        if persistent.archive_run_id is None:
            if not stage_exists:
                return False
            raise WeiboError("渲染阶段缺少精确同步记录标识", kind=WeiboErrorKind.PARSE)
        if persistent.legacy_index_sha256 is None:
            if not stage_exists:
                audit = Path(persistent.output_dir) / ".work" / "legacy"
                if not audit.exists() and not audit.is_symlink():
                    return False
            raise WeiboError("持久任务缺少可信旧索引摘要", kind=WeiboErrorKind.PARSE)
        audit_completed = legacy_finalize_completed(
            persistent.output_dir,
            persistent.task_id,
            expected_uid,
            persistent.archive_run_id,
            persistent.mode,
            persistent.legacy_index_sha256,
        )
        if not stage_exists and not audit_completed:
            return False
        if stage_exists:
            legacy_uid = staged_legacy_archive_uid(
                persistent.output_dir,
                persistent.task_id,
            )
            if legacy_uid != expected_uid:
                raise WeiboError("旧版备份账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
        inspection = self.inspector(
            persistent.output_dir,
            current_uid=expected_uid,
        )
        if inspection.state != "archive" or inspection.uid != expected_uid:
            raise WeiboError("渲染阶段正式归档无法安全确认", kind=WeiboErrorKind.PARSE)
        root = _physical_root(Path(persistent.output_dir))
        repository = ArchiveRepository.open(root, expected_uid)
        try:
            sync_run = repository.get_sync_run(persistent.archive_run_id)
            if sync_run.mode != persistent.mode:
                raise WeiboError("精确同步记录模式与持久任务不一致", kind=WeiboErrorKind.PARSE)
            if sync_run.status != "done":
                raise WeiboError("精确同步记录尚未完成", kind=WeiboErrorKind.PARSE)
        finally:
            repository.close()
        return True

    async def _ensure_legacy_index_sha256(self, task_id: str, persistent):
        if persistent.mode != "create":
            return persistent
        if not staged_legacy_archive_exists(persistent.output_dir, task_id):
            return persistent
        value = await asyncio.to_thread(
            staged_legacy_archive_sha256,
            persistent.output_dir,
            task_id,
        )
        if (
            persistent.legacy_index_sha256 is not None
            and persistent.legacy_index_sha256 != value
        ):
            raise WeiboError("旧索引摘要与持久任务不一致", kind=WeiboErrorKind.PARSE)
        if persistent.legacy_index_sha256 is None:
            record = self.manager.get(task_id)
            if record is None or record.persistent_record is None:
                updated = replace(
                    persistent,
                    saved_at=datetime.now(timezone.utc).isoformat(),
                    legacy_index_sha256=value,
                )
                if self.manager._persistent_store is None:
                    raise WeiboError("持久任务存储尚未配置", kind=WeiboErrorKind.API)
                self.manager._persistent_store.save(updated)
                return updated
            await self.manager.set_legacy_index_sha256(task_id, value)
            return record.persistent_record
        return persistent

    def _local_resume_uid(self, persistent) -> str:
        if persistent.phase == "render" or persistent.mode == "incremental":
            root = _physical_root(Path(persistent.output_dir))
            inspection = self.inspector(str(root), current_uid="")
            if inspection.state != "uid_mismatch" or not inspection.uid:
                raise WeiboError("无法确认可恢复归档的账号标识", kind=WeiboErrorKind.PARSE)
            return inspection.uid
        legacy_uid = self._staged_legacy_uid_if_present(
            persistent.output_dir,
            persistent.task_id,
        )
        if legacy_uid is not None:
            return legacy_uid
        root = _physical_root(Path(persistent.output_dir))
        temporary = root.parent / (
            f".{root.name}.{persistent.mode}-task-{persistent.task_id}"
        )
        target = temporary if temporary.exists() or temporary.is_symlink() else root
        inspection = self.inspector(str(target), current_uid="")
        if inspection.state != "uid_mismatch" or not inspection.uid:
            raise WeiboError("无法确认可恢复归档的账号标识", kind=WeiboErrorKind.PARSE)
        return inspection.uid

    @staticmethod
    def _validate_task_tree(path: Path, expected_parent: Path) -> None:
        if path.parent.resolve(strict=True) != expected_parent.resolve(strict=True):
            raise WeiboError("任务清理路径越界", kind=WeiboErrorKind.API)
        marker = path.lstat()
        if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
            raise WeiboError("任务清理目标不是安全目录", kind=WeiboErrorKind.API)
        for child in path.rglob("*"):
            child_marker = child.lstat()
            if stat.S_ISLNK(child_marker.st_mode) or not (
                stat.S_ISDIR(child_marker.st_mode)
                or (stat.S_ISREG(child_marker.st_mode) and child_marker.st_nlink == 1)
            ):
                raise WeiboError("任务清理目录包含异常文件类型", kind=WeiboErrorKind.API)

    @staticmethod
    def _fsync_parent(parent: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


personal_archive_tasks = PersonalArchiveTaskService()
