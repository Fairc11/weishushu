"""关注资料独立持久任务服务。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from backend.app.schemas import ArchiveFolderInspection, FollowingArchiveRequest
from backend.app.services.archive_folder import inspect_archive_folder
from backend.app.services.system_power import KeepAwakeLease, system_power_service
from backend.app.services.task_manager import TaskManager, run_in_background, task_manager
from weibo_book import WeiboBook
from weibo_book.archive.following_source import (
    CrawlClientFollowingRequest,
    FollowingSource,
)
from weibo_book.archive.following_sync import FollowingArchiveSync
from weibo_book.archive.pacing import AdaptiveRequestScheduler, PacingStatus
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.source import WeiboArchiveSource
from weibo_book.archive.sync import _archive_lock, _physical_root
from weibo_book.errors import OperationCancelled, WeiboError, WeiboErrorKind
from weibo_book.extractor import WeiboExtractor


@dataclass(frozen=True)
class FollowingTaskStartInfo:
    task_id: str
    self_uid: str
    self_screen_name: str
    worker: asyncio.Task


def build_following_source(
    self_uid: str,
    *,
    pacing_scheduler: AdaptiveRequestScheduler | None = None,
) -> FollowingSource:
    book = WeiboBook()
    cookie_str = book.ensure_login(force=False)
    if not cookie_str:
        raise WeiboError("未登录或登录态已过期", kind=WeiboErrorKind.AUTH)
    extractor = WeiboExtractor(
        cookie_str=cookie_str,
        image_quality=book.image_quality,
        low_intensity=(
            pacing_scheduler is not None and pacing_scheduler.is_low_intensity
        ),
    )
    session_source = WeiboArchiveSource(
        extractor,
        self_uid=self_uid,
        image_quality=book.image_quality,
        pacing_scheduler=pacing_scheduler,
    )
    return FollowingSource(
        CrawlClientFollowingRequest(extractor.client),
        self_uid=self_uid,
        session_probe=session_source.probe_session,
    )


class FollowingArchiveTaskService:
    def __init__(
        self,
        *,
        manager: TaskManager = task_manager,
        source_builder: Callable[..., Any] = build_following_source,
        sync_factory: Callable[..., Any] = FollowingArchiveSync,
        inspector: Callable[..., ArchiveFolderInspection] = inspect_archive_folder,
    ) -> None:
        self.manager = manager
        self.source_builder = source_builder
        self.sync_factory = sync_factory
        self.inspector = inspector

    @staticmethod
    def _identity(identity: dict[str, str]) -> tuple[str, str]:
        uid = identity.get("uid") if isinstance(identity, dict) else None
        name = identity.get("screen_name") if isinstance(identity, dict) else None
        if (
            not isinstance(uid, str)
            or not uid.isdigit()
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise WeiboError("当前登录账号信息无效", kind=WeiboErrorKind.AUTH)
        return uid, name

    @staticmethod
    def _require_archive(inspection: ArchiveFolderInspection, uid: str) -> None:
        if inspection.state == "uid_mismatch" or (inspection.uid and inspection.uid != uid):
            raise WeiboError("当前账号与微博书归档账号不一致", kind=WeiboErrorKind.AUTH)
        if inspection.state != "archive":
            raise WeiboError("更新关注资料必须使用现有微博书目录", kind=WeiboErrorKind.API)

    async def start(
        self,
        request: FollowingArchiveRequest,
        identity: dict[str, str],
    ) -> FollowingTaskStartInfo:
        uid, name = self._identity(identity)
        inspection = await asyncio.to_thread(
            self.inspector, request.output_dir, current_uid=uid
        )
        self._require_archive(inspection, uid)
        root = _physical_root(Path(request.output_dir))
        snapshot_id = str(uuid.uuid4())
        try:
            task_id = await self.manager.create_following_archive(
                output_dir=str(root),
                expected_uid=uid,
                snapshot_id=snapshot_id,
                pacing_mode=request.pacing_mode,
                keep_awake_when_plugged=request.keep_awake_when_plugged,
            )
            task_record = self.manager.get(task_id)
            if task_record is None or task_record.persistent_record is None:
                raise WeiboError("关注资料持久任务未正确建立", kind=WeiboErrorKind.API)
            with _archive_lock(root):
                repository = ArchiveRepository.open(root, uid)
                try:
                    repository.begin_following_snapshot(
                        task_record.persistent_record.started_at, snapshot_id
                    )
                finally:
                    repository.close()
        except BaseException:
            if "task_id" in locals():
                await self.manager.discard_unstarted_following_archive(task_id)
            raise
        return self._launch(task_id, uid, name)

    @staticmethod
    def _is_initial_checkpoint(checkpoint: dict[str, object]) -> bool:
        return checkpoint == {
            "snapshot_id": checkpoint.get("snapshot_id"),
            "blogger_next_page": 1,
            "blogger_next_cursor": None,
            "blogger_completed_count": 0,
            "bloggers_done": False,
            "supertopics_done": False,
            "blogger_reported_total": None,
            "supertopic_reported_total": None,
        }

    def _repair_missing_initial_snapshot(self, persistent) -> None:
        root = _physical_root(Path(persistent.output_dir))
        snapshot_id = persistent.checkpoint["snapshot_id"]
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, persistent.expected_uid)
            try:
                if repository.following_snapshot_exists(snapshot_id):
                    return
                if not self._is_initial_checkpoint(persistent.checkpoint):
                    raise WeiboError(
                        "关注资料恢复点已推进，但暂存快照不存在",
                        kind=WeiboErrorKind.PARSE,
                    )
                repository.begin_following_snapshot(persistent.started_at, snapshot_id)
            finally:
                repository.close()

    async def resume(
        self,
        task_id: str,
        identity: dict[str, str],
    ) -> FollowingTaskStartInfo:
        uid, name = self._identity(identity)
        record = self.manager.get(task_id)
        if (
            record is None
            or record.persistent_record is None
            or record.persistent_record.task_kind != "following_archive"
        ):
            raise WeiboError("未找到可恢复的关注资料任务", kind=WeiboErrorKind.API)
        persistent = record.persistent_record
        if persistent.expected_uid != uid:
            raise WeiboError("当前账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
        inspection = await asyncio.to_thread(
            self.inspector, persistent.output_dir, current_uid=uid
        )
        self._require_archive(inspection, uid)
        await asyncio.to_thread(self._repair_missing_initial_snapshot, persistent)
        if not await self.manager.prepare_persistent_resume(task_id):
            raise WeiboError("当前任务状态不允许恢复", kind=WeiboErrorKind.API)
        return self._launch(task_id, uid, name)

    def _launch(self, task_id: str, uid: str, name: str) -> FollowingTaskStartInfo:
        worker = asyncio.create_task(
            run_in_background(
                task_id,
                lambda: self._execute(task_id, uid),
                manager=self.manager,
                persistent_cancel_handler=self.finish_accepted_cancel,
            )
        )
        record = self.manager.get(task_id)
        if record is not None:
            record._asyncio_task = worker
        return FollowingTaskStartInfo(task_id, uid, name, worker)

    async def _execute(self, task_id: str, uid: str) -> dict[str, object]:
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            raise WeiboError("关注资料持久任务不存在", kind=WeiboErrorKind.API)
        persistent = record.persistent_record
        loop = asyncio.get_running_loop()
        keep_awake = KeepAwakeLease(
            system_power_service,
            enabled=(
                persistent.keep_awake_when_plugged
                and persistent.pacing_mode != "standard"
            ),
            reason="低强度关注资料更新",
        )
        keep_awake.refresh()

        def phase(value: str) -> None:
            asyncio.run_coroutine_threadsafe(
                self.manager.set_persistent_phase(task_id, value), loop
            ).result()

        def checkpoint(value: dict[str, object]) -> None:
            asyncio.run_coroutine_threadsafe(
                self.manager.set_following_checkpoint(
                    task_id,
                    blogger_next_page=value["blogger_next_page"],
                    blogger_next_cursor=value["blogger_next_cursor"],
                    blogger_completed_count=value["blogger_completed_count"],
                    bloggers_done=value["bloggers_done"],
                    supertopics_done=value["supertopics_done"],
                    blogger_reported_total=value["blogger_reported_total"],
                    supertopic_reported_total=value["supertopic_reported_total"],
                ),
                loop,
            ).result()

        def progress(value: dict[str, object]) -> None:
            asyncio.run_coroutine_threadsafe(
                self.manager.update_progress_event(task_id, value), loop
            ).result()

        def pacing_status(value: PacingStatus) -> None:
            asyncio.run_coroutine_threadsafe(
                self.manager.update_pacing_status(task_id, value), loop
            ).result()

        scheduler = AdaptiveRequestScheduler(
            persistent.pacing_mode,
            pause_event=record._pause_event,
            cancel_event=record._cancel_event,
            status_callback=pacing_status,
        )
        scheduler.set_known_request_counts(
            profile=(0 if persistent.checkpoint["bloggers_done"] else 1)
            + (0 if persistent.checkpoint["supertopics_done"] else 1)
        )

        def power_snapshot():
            snapshot = system_power_service.snapshot()
            keep_awake.refresh(snapshot)
            return snapshot

        scheduler.set_power_snapshot_provider(power_snapshot)
        root = _physical_root(Path(persistent.output_dir))
        try:
            def run_sync():
                source = self.source_builder(uid, pacing_scheduler=scheduler)
                wake_probe = getattr(source, "probe_session", None)
                if callable(wake_probe):
                    scheduler.set_wake_probe(wake_probe)
                with _archive_lock(root):
                    repository = ArchiveRepository.open(root, uid)
                    try:
                        sync = self.sync_factory(
                            repository,
                            source,
                            checkpoint_saved=checkpoint,
                            phase_changed=phase,
                            progress_callback=progress,
                            cancel_requested=record._cancel_event.is_set,
                            pause_requested=record._pause_event.is_set,
                            pacing_scheduler=scheduler,
                            begin_commit=record.try_begin_commit,
                        )
                        return sync.run(dict(record.persistent_record.checkpoint))
                    except OperationCancelled:
                        self._discard_if_staging(
                            repository,
                            persistent.checkpoint["snapshot_id"],
                            persistent.checkpoint,
                        )
                        raise
                    finally:
                        repository.close()

            result = await asyncio.to_thread(run_sync)
            return asdict(result) | {
                "task_kind": "following_archive",
                "output_dir": str(root),
                "duration_source": "local_minimum",
            }
        finally:
            scheduler.close()
            keep_awake.close()

    @classmethod
    def _discard_if_staging(
        cls,
        repository: ArchiveRepository,
        snapshot_id: str,
        checkpoint: dict[str, object],
    ) -> None:
        if not repository.following_snapshot_exists(snapshot_id):
            if cls._is_initial_checkpoint(checkpoint):
                return
            raise WeiboError(
                "关注资料恢复点已推进，但暂存快照不存在",
                kind=WeiboErrorKind.PARSE,
            )
        snapshot = repository.get_following_snapshot(snapshot_id)
        if snapshot.status == "staging":
            repository.discard_following_snapshot(snapshot_id)

    async def pause(self, task_id: str) -> bool:
        return await self.manager.request_pause(task_id)

    async def cancel(self, task_id: str) -> bool:
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            return False
        if record.state not in {"waiting_resume", "error"}:
            return await self.manager.request_cancel(task_id)
        await self._discard_persistent_snapshot(record.persistent_record)
        await self.manager.set_cancelled(task_id)
        return True

    async def abandon(self, task_id: str, identity: dict[str, str]) -> bool:
        uid, _ = self._identity(identity)
        record = self.manager.get(task_id)
        if (
            record is None
            or record.persistent_record is None
            or record.persistent_record.task_kind != "following_archive"
            or record.state not in {"waiting_resume", "error"}
        ):
            return False
        if record.persistent_record.expected_uid != uid:
            raise WeiboError("当前账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
        await self._discard_persistent_snapshot(record.persistent_record)
        await self.manager.set_abandoned(task_id)
        return True

    async def _discard_persistent_snapshot(self, persistent) -> None:
        root = _physical_root(Path(persistent.output_dir))
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, persistent.expected_uid)
            try:
                self._discard_if_staging(
                    repository,
                    persistent.checkpoint["snapshot_id"],
                    persistent.checkpoint,
                )
            finally:
                repository.close()

    async def finish_interrupted_cancel(self, persistent) -> None:
        if persistent.task_kind != "following_archive" or persistent.state != "cancelling":
            return
        await self._discard_persistent_snapshot(persistent)
        self.manager._persistent_store.clear()

    async def finish_accepted_cancel(self, task_id: str) -> None:
        record = self.manager.get(task_id)
        if record is None or record.persistent_record is None:
            return
        await self.finish_interrupted_cancel(record.persistent_record)
        await self.manager.set_cancelled(task_id)


following_archive_tasks = FollowingArchiveTaskService()
