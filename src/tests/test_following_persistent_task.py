from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest


def _checkpoint(snapshot_id="11111111-1111-4111-8111-111111111111"):
    return {
        "snapshot_id": snapshot_id,
        "blogger_next_page": 1,
        "blogger_next_cursor": None,
        "blogger_completed_count": 0,
        "bloggers_done": False,
        "supertopics_done": False,
        "blogger_reported_total": None,
        "supertopic_reported_total": None,
    }


def test_following_record_round_trip_uses_exact_task_combination(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskRecord,
        PersistentTaskStore,
    )

    now = "2026-07-18T00:00:00+00:00"
    record = PersistentTaskRecord(
        schema_version=7,
        task_id="0123456789ab",
        task_kind="following_archive",
        mode="update",
        output_dir=str((tmp_path / "微博书").resolve()),
        state="running",
        phase="bloggers",
        archive_run_id=None,
        progress_current=0,
        progress_total=None,
        progress_unit="page",
        started_at=now,
        saved_at=now,
        pause_reason="",
        saved_content="尚未提交关注资料",
        expected_uid="10001",
        legacy_index_sha256=None,
        error_recoverable=False,
        pacing_mode="low_2_3_hours",
        keep_awake_when_plugged=True,
        pacing_state="estimating",
        pacing_request_kind=None,
        next_wait_seconds=None,
        checkpoint=_checkpoint(),
    )
    store = PersistentTaskStore(tmp_path / "task.json")
    store.save(record)
    assert store.load() == record


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "incremental"},
        {"phase": "sync"},
        {"archive_run_id": "11111111-1111-4111-8111-111111111111"},
        {"expected_uid": "not-numeric"},
        {"checkpoint": {}},
        {"checkpoint": _checkpoint() | {"unknown": True}},
        {"checkpoint": _checkpoint() | {"blogger_next_page": 0}},
        {"checkpoint": _checkpoint() | {"blogger_next_cursor": "50"}},
    ],
)
def test_following_record_rejects_invalid_cross_task_values(tmp_path, changes):
    from backend.app.services.persistent_task_store import (
        PersistentTaskRecord,
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    now = "2026-07-18T00:00:00+00:00"
    record = PersistentTaskRecord(
        6, "0123456789ab", "following_archive", "update",
        str((tmp_path / "微博书").resolve()), "running", "bloggers", None,
        0, None, "page", now, now, "", "尚未提交关注资料", "10001",
        None, False, "standard", False, "standard", None, None, _checkpoint(),
    )
    with pytest.raises(PersistentTaskStoreError):
        PersistentTaskStore(tmp_path / "task.json").save(replace(record, **changes))


def test_manager_creates_following_task_and_advances_checkpoint(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)

    async def scenario():
        task_id = await manager.create_following_archive(
            output_dir=str((tmp_path / "微博书").resolve()),
            expected_uid="10001",
            snapshot_id="11111111-1111-4111-8111-111111111111",
            pacing_mode="standard",
            keep_awake_when_plugged=False,
        )
        await manager.set_following_checkpoint(
            task_id,
            blogger_next_page=2,
            blogger_next_cursor=50,
            blogger_completed_count=1,
            bloggers_done=False,
            supertopics_done=False,
            blogger_reported_total=2,
            supertopic_reported_total=None,
        )
        return task_id

    task_id = asyncio.run(scenario())
    record = manager.get(task_id).persistent_record
    assert record is not None
    assert record.task_kind == "following_archive"
    assert record.checkpoint == _checkpoint() | {
        "blogger_next_page": 2,
        "blogger_next_cursor": 50,
        "blogger_completed_count": 1,
        "blogger_reported_total": 2,
    }


def test_manager_rejects_checkpoint_snapshot_change_and_page_regression(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.errors import WeiboError

    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))

    async def scenario():
        task_id = await manager.create_following_archive(
            output_dir=str((tmp_path / "微博书").resolve()),
            expected_uid="10001",
            snapshot_id="11111111-1111-4111-8111-111111111111",
        )
        await manager.set_following_checkpoint(
            task_id, blogger_next_page=2, blogger_next_cursor=50,
            blogger_completed_count=1,
            bloggers_done=False, supertopics_done=False,
            blogger_reported_total=2, supertopic_reported_total=None,
        )
        with pytest.raises(WeiboError, match="倒退"):
            await manager.set_following_checkpoint(
                task_id, blogger_next_page=1, blogger_next_cursor=None,
                blogger_completed_count=0,
                bloggers_done=False, supertopics_done=False,
                blogger_reported_total=None, supertopic_reported_total=None,
            )

    asyncio.run(scenario())
