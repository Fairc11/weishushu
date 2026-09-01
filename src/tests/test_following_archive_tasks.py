from __future__ import annotations

import asyncio

import pytest

from backend.app.schemas import FollowingArchiveRequest
from weibo_book.archive.following import FollowingObjectRecord
from weibo_book.archive.following_source import BloggerPage, FollowingListResult
from weibo_book.errors import WeiboError, WeiboErrorKind


def _archive(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    root = tmp_path / "微博书"
    ArchiveRepository.create(root, "10001", "本人").close()
    return root


class CompleteSource:
    def fetch_blogger_page(self, **_kwargs):
        return BloggerPage([
            FollowingObjectRecord(
                "blogger", "20001", "甲", "https://weibo.com/u/20001", "", 0
            )
        ], 1, 0, False)

    def fetch_supertopics(self):
        return FollowingListResult([
            FollowingObjectRecord(
                "supertopic", "1022:1", "超话甲", "//weibo.com/p/1",
                "sinaweibo://pageinfo?containerid=1", 0,
            )
        ], 1, True)


def _request(root):
    return FollowingArchiveRequest(
        output_dir=str(root),
        pacing_mode="standard",
        keep_awake_when_plugged=False,
    )


def test_service_runs_independent_update_to_completion(tmp_path):
    from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.repository import ArchiveRepository

    root = _archive(tmp_path)
    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    service = FollowingArchiveTaskService(
        manager=manager,
        source_builder=lambda _uid, **_kwargs: CompleteSource(),
    )

    async def scenario():
        started = await service.start(
            _request(root), {"uid": "10001", "screen_name": "本人"}
        )
        await started.worker
        return manager.snapshot(started.task_id)

    snapshot = asyncio.run(scenario())
    repository = ArchiveRepository.open(root, "10001")
    try:
        assert snapshot["state"] == "done"
        assert snapshot["result"]["blogger_count"] == 1
        assert snapshot["result"]["duration_source"] == "local_minimum"
        assert snapshot["result"]["task_kind"] == "following_archive"
        assert snapshot["result"]["output_dir"] == str(root)
        assert repository.get_current_following_snapshot() is not None
    finally:
        repository.close()


def test_rate_limit_pauses_and_abandon_discards_only_staging(tmp_path):
    from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.repository import ArchiveRepository

    class LimitedSource(CompleteSource):
        def fetch_blogger_page(self, **_kwargs):
            raise WeiboError("精确限流", kind=WeiboErrorKind.RATE_LIMIT)

    root = _archive(tmp_path)
    repository = ArchiveRepository.open(root, "10001")
    old = repository.begin_following_snapshot("2026-07-17T00:00:00+00:00")
    repository.stage_following_items(old, [
        FollowingObjectRecord(
            "blogger", "20000", "旧对象", "https://weibo.com/u/20000", "", 0
        ),
        FollowingObjectRecord(
            "supertopic", "1022:0", "旧超话", "//weibo.com/p/0",
            "sinaweibo://pageinfo?containerid=0", 0,
        ),
    ])
    repository.commit_following_snapshot(
        old, cutoff_at="2026-07-17T01:00:00+00:00",
        bloggers_complete=True, supertopics_complete=True,
        blogger_reported_total=1, supertopic_reported_total=1,
    )
    repository.close()
    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    service = FollowingArchiveTaskService(
        manager=manager,
        source_builder=lambda _uid, **_kwargs: LimitedSource(),
    )

    async def scenario():
        started = await service.start(
            _request(root), {"uid": "10001", "screen_name": "本人"}
        )
        await started.worker
        paused = manager.snapshot(started.task_id)
        assert await service.abandon(
            started.task_id, {"uid": "10001", "screen_name": "本人"}
        )
        return paused, manager.snapshot(started.task_id)

    paused, abandoned = asyncio.run(scenario())
    repository = ArchiveRepository.open(root, "10001")
    try:
        assert paused["state"] == "waiting_resume"
        assert paused["error"] is None
        assert abandoned["state"] == "abandoned"
        assert repository.get_current_following_snapshot().snapshot_id == old
        staging_id = paused["result"] if paused["result"] else None
        assert staging_id is None
    finally:
        repository.close()


def test_resume_repairs_task_record_created_before_staging_snapshot(tmp_path):
    from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager

    root = _archive(tmp_path)
    store = PersistentTaskStore(tmp_path / "task.json")
    first_manager = TaskManager(store)

    async def create_record_only():
        return await first_manager.create_following_archive(
            output_dir=str(root),
            expected_uid="10001",
            snapshot_id="11111111-1111-4111-8111-111111111111",
        )

    task_id = asyncio.run(create_record_only())
    manager = TaskManager(store)
    service = FollowingArchiveTaskService(
        manager=manager,
        source_builder=lambda _uid, **_kwargs: CompleteSource(),
    )

    async def resume_after_restart():
        recovered = await manager.reconcile_after_process_start()
        assert recovered is not None
        started = await service.resume(
            task_id, {"uid": "10001", "screen_name": "本人"}
        )
        await started.worker
        return manager.snapshot(task_id)

    snapshot = asyncio.run(resume_after_restart())
    assert snapshot["state"] == "done"
    assert snapshot["result"]["blogger_count"] == 1


@pytest.mark.parametrize("action", ["abandon", "cancel"])
def test_record_created_before_snapshot_can_be_cleared_without_resume(tmp_path, action):
    from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager

    root = _archive(tmp_path)
    store = PersistentTaskStore(tmp_path / "task.json")
    first_manager = TaskManager(store)

    async def create_record_only():
        return await first_manager.create_following_archive(
            output_dir=str(root),
            expected_uid="10001",
            snapshot_id="11111111-1111-4111-8111-111111111111",
        )

    task_id = asyncio.run(create_record_only())
    manager = TaskManager(store)
    service = FollowingArchiveTaskService(manager=manager)

    async def clear_after_restart():
        recovered = await manager.reconcile_after_process_start()
        assert recovered is not None
        if action == "abandon":
            return await service.abandon(
                task_id, {"uid": "10001", "screen_name": "本人"}
            )
        return await service.cancel(task_id)

    assert asyncio.run(clear_after_restart()) is True
    assert store.load() is None


def test_start_snapshot_failure_rolls_back_unstarted_task_record(tmp_path, monkeypatch):
    from backend.app.services.following_archive_tasks import FollowingArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = _archive(tmp_path)
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    service = FollowingArchiveTaskService(
        manager=manager,
        source_builder=lambda _uid, **_kwargs: CompleteSource(),
    )
    monkeypatch.setattr(
        ArchiveRepository,
        "begin_following_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ArchiveError("精确写入失败")),
    )

    async def scenario():
        with pytest.raises(ArchiveError, match="精确写入失败"):
            await service.start(
                _request(root), {"uid": "10001", "screen_name": "本人"}
            )

    asyncio.run(scenario())
    assert store.load() is None
