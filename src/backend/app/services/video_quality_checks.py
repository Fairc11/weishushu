"""旧视频一次性核验后台任务；不下载媒体，也不启动正式备份。"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from weibo_book.archive import video_quality
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.pacing import AdaptiveRequestScheduler
from weibo_book.archive.sync import _archive_lock, _physical_root
from weibo_book.errors import OperationCancelled, OperationPaused, WeiboError
from .task_manager import run_in_background


def _local_summary(output_dir: str, uid: str) -> dict[str, int]:
    root = _physical_root(Path(output_dir))
    with _archive_lock(root):
        repository = ArchiveRepository.open(root, uid)
        try:
            return video_quality.summary(repository)
        finally:
            repository.close()


async def start_check(output_dir: str, uid: str, login_uid: str, *, manager, dependency_builder, pacing_mode="standard") -> dict:
    summary = await asyncio.to_thread(_local_summary, output_dir, uid)
    if summary["unchecked_count"] + summary["failed_count"] == 0:
        return {"task_id": None, "summary": summary}
    task_id = await manager.create()
    record = manager.get(task_id)
    record.control_mode = "cooperative"
    loop = asyncio.get_running_loop()
    notifications = []

    def progress(current: int, total: int) -> None:
        notifications.append(asyncio.run_coroutine_threadsafe(
            manager.update_progress(task_id, current / max(total, 1), f"正在检查旧视频画质 {current}/{total}"), loop
        ))

    def check() -> dict:
        root = _physical_root(Path(output_dir))
        with _archive_lock(root):
            repository = ArchiveRepository.open(root, uid)
            try:
                if record._cancel_event.is_set():
                    raise OperationCancelled("已取消旧视频画质检查")
                scheduler = AdaptiveRequestScheduler(pacing_mode, cancel_event=record._cancel_event)
                try:
                    scheduler.set_known_request_counts(detail=summary["unchecked_count"] + summary["failed_count"])
                    parameters = inspect.signature(dependency_builder).parameters
                    options = {"login_uid": login_uid} if "login_uid" in parameters else {}
                    if "pacing_scheduler" in parameters:
                        options["pacing_scheduler"] = scheduler
                    source, _stager = dependency_builder(uid, **options)
                    return video_quality.scan(repository, source, cancel_requested=record._cancel_event.is_set, progress=progress)
                finally:
                    scheduler.close()
            finally:
                repository.close()

    async def run() -> dict:
        await manager.update_progress(task_id, 0.0, "正在检查未核验的旧视频画质")
        worker = asyncio.create_task(asyncio.to_thread(check))
        try:
            result = await asyncio.shield(worker)
            if notifications:
                await asyncio.gather(*(asyncio.wrap_future(item) for item in notifications))
            if record._cancel_event.is_set():
                raise OperationCancelled("已取消旧视频画质检查")
            return result
        except OperationPaused as exc:
            raise WeiboError("旧视频画质检查暂时无法继续，请稍后重试", kind=exc.kind) from exc
        except asyncio.CancelledError:
            record._cancel_event.set()
            try:
                await asyncio.shield(worker)
            except OperationCancelled:
                pass
            raise

    record._asyncio_task = asyncio.create_task(run_in_background(task_id, run, manager=manager))
    return {"task_id": task_id, "summary": None}
