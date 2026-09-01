"""阶段 2 低强度档位的持久记录与任务服务契约。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _schema_four_payload(tmp_path: Path, **changes) -> dict:
    payload = {
        "schema_version": 4,
        "task_id": "0123456789ab",
        "task_kind": "personal_archive",
        "mode": "incremental",
        "output_dir": str((tmp_path / "微博书").resolve()),
        "state": "waiting_resume",
        "phase": "sync",
        "archive_run_id": None,
        "progress_current": 3,
        "progress_total": 10,
        "progress_unit": "post",
        "started_at": "2026-07-17T00:00:00+00:00",
        "saved_at": "2026-07-17T00:01:00+00:00",
        "pause_reason": "unexpected_exit",
        "saved_content": "已保存 3 条微博",
        "expected_uid": "10001",
        "legacy_index_sha256": None,
        "error_recoverable": False,
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_schema_one_through_four_migrate_atomically_to_current_schema(
    tmp_path,
    schema_version,
):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "task.json"
    payload = _schema_four_payload(tmp_path, schema_version=schema_version)
    if schema_version == 1:
        payload.pop("expected_uid")
        payload.pop("legacy_index_sha256")
        payload.pop("error_recoverable")
    elif schema_version == 2:
        payload.pop("legacy_index_sha256")
        payload.pop("error_recoverable")
    elif schema_version == 3:
        payload.pop("error_recoverable")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    record = PersistentTaskStore(path).load()
    rewritten = json.loads(path.read_text(encoding="utf-8"))

    assert record is not None
    assert record.schema_version == 7
    assert record.checkpoint == {}
    assert record.pacing_mode == "standard"
    assert record.keep_awake_when_plugged is False
    assert record.pacing_state == "standard"
    assert record.pacing_request_kind is None
    assert record.next_wait_seconds is None
    assert rewritten == record.__dict__


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pacing_mode": "slow"}, "无效"),
        ({"keep_awake_when_plugged": 1}, "无效"),
        ({"pacing_state": "sleeping"}, "无效"),
        ({"pacing_request_kind": "post"}, "无效"),
        ({"next_wait_seconds": -1.0}, "无效"),
        ({"next_wait_seconds": True}, "无效"),
        ({"next_wait_seconds": float("inf")}, "无效"),
    ],
)
def test_schema_six_rejects_invalid_pacing_values(tmp_path, changes, message):
    from backend.app.services.persistent_task_store import (
        PersistentTaskRecord,
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    values = {
        **_schema_four_payload(tmp_path),
        "schema_version": 7,
        "pacing_mode": "low_2_3_hours",
        "keep_awake_when_plugged": True,
        "pacing_state": "waiting",
        "pacing_request_kind": "detail",
        "next_wait_seconds": 12.5,
        "checkpoint": {},
        "target_label": None,
        **changes,
    }
    record = PersistentTaskRecord(**values)

    with pytest.raises(PersistentTaskStoreError, match=message):
        PersistentTaskStore(tmp_path / "invalid.json").save(record)


def test_personal_archive_request_requires_strict_pacing_fields():
    from pydantic import ValidationError

    from backend.app.schemas import PersonalArchiveRequest

    with pytest.raises(ValidationError):
        PersonalArchiveRequest(output_dir="/tmp/archive", mode="create")
    with pytest.raises(ValidationError):
        PersonalArchiveRequest(
            output_dir="/tmp/archive",
            mode="create",
            pacing_mode="standard",
            keep_awake_when_plugged=1,
        )

    request = PersonalArchiveRequest(
        output_dir="/tmp/archive",
        mode="create",
        pacing_mode="low_4_6_hours",
        keep_awake_when_plugged=True,
    )

    assert request.pacing_mode == "low_4_6_hours"
    assert request.keep_awake_when_plugged is True


def test_task_creation_persists_selected_pacing_and_recovery_keeps_it(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)

    async def scenario():
        task_id = await manager.create_personal_archive(
            mode="incremental",
            output_dir=str((tmp_path / "微博书").resolve()),
            expected_uid="10001",
            pacing_mode="low_8_12_hours",
            keep_awake_when_plugged=True,
        )
        await manager.set_waiting_resume(task_id, pause_reason="user_requested")
        summary = manager.recovery_summary()
        assert summary is not None
        assert summary["pacing_mode"] == "low_8_12_hours"
        assert summary["keep_awake_when_plugged"] is True
        assert await manager.prepare_persistent_resume(task_id) is True
        record = manager.get(task_id).persistent_record
        assert record is not None
        return record

    record = asyncio.run(scenario())

    assert record.pacing_mode == "low_8_12_hours"
    assert record.keep_awake_when_plugged is True


@pytest.mark.parametrize(
    ("pacing_mode", "active_state", "expected_state"),
    [
        ("standard", "standard", "standard"),
        ("low_2_3_hours", "requesting", "paused"),
    ],
)
def test_waiting_resume_normalizes_pacing_state(
    tmp_path,
    pacing_mode,
    active_state,
    expected_state,
):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.pacing import PacingStatus

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)

    async def scenario():
        task_id = await manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "微博书").resolve()),
            pacing_mode=pacing_mode,
            keep_awake_when_plugged=False,
        )
        await manager.update_pacing_status(task_id, PacingStatus(
            mode=pacing_mode,
            state=active_state,
            request_kind=None if pacing_mode == "standard" else "profile",
            next_wait_seconds=None,
            target_min_seconds=None if pacing_mode == "standard" else 7200,
            target_max_seconds=None if pacing_mode == "standard" else 10800,
            disclaimer="目标区间不是完成时间承诺",
        ))
        await manager.set_waiting_resume(task_id, pause_reason="user_requested")
        return manager.snapshot(task_id), manager.recovery_summary(), store.load()

    snapshot, summary, persistent = asyncio.run(scenario())

    assert snapshot["state"] == "waiting_resume"
    assert snapshot["pacing_state"] == expected_state
    assert snapshot["next_wait_seconds"] is None
    assert summary is not None
    assert summary["pacing_state"] == expected_state
    assert summary["next_wait_seconds"] is None
    assert persistent is not None
    assert persistent.pacing_state == expected_state
    assert persistent.next_wait_seconds is None


def test_update_pacing_status_persists_and_broadcasts_without_touching_progress(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.pacing import PacingStatus

    manager = TaskManager(persistent_store=PersistentTaskStore(tmp_path / "task.json"))

    async def scenario():
        task_id = await manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "微博书").resolve()),
            expected_uid="10001",
            pacing_mode="low_2_3_hours",
            keep_awake_when_plugged=False,
        )
        record = manager.get(task_id)
        assert record is not None
        await manager.update_progress_event(task_id, {
            "pct": 0.4,
            "detail": "已保存 4 条微博",
            "current": 4,
            "total": 10,
            "unit": "post",
        })
        queue = await manager.subscribe(task_id)
        assert queue is not None
        await queue.get()
        await manager.update_pacing_status(task_id, PacingStatus(
            mode="low_2_3_hours",
            state="waiting",
            request_kind="comments",
            next_wait_seconds=120.0,
            target_min_seconds=7200,
            target_max_seconds=10800,
            disclaimer="目标区间不是完成时间承诺",
        ))
        event = await queue.get()
        return manager.get(task_id), manager.snapshot(task_id), event

    record, snapshot, event = asyncio.run(scenario())

    assert record is not None and record.persistent_record is not None
    assert record.persistent_record.progress_current == 4
    assert record.persistent_record.progress_total == 10
    assert record.persistent_record.saved_content == "已保存 4 条微博"
    assert record.persistent_record.pacing_state == "waiting"
    assert record.persistent_record.pacing_request_kind == "comments"
    assert record.persistent_record.next_wait_seconds == 120.0
    assert snapshot["pacing_mode"] == "low_2_3_hours"
    assert snapshot["pacing_state"] == "waiting"
    assert snapshot["pacing_request_kind"] == "comments"
    assert snapshot["next_wait_seconds"] == 120.0
    assert event == {
        "type": "pacing",
        "mode": "low_2_3_hours",
        "state": "waiting",
        "request_kind": "comments",
        "next_wait_seconds": 120.0,
        "target_min_seconds": 7200,
        "target_max_seconds": 10800,
        "disclaimer": "目标区间不是完成时间承诺",
    }


def test_update_pacing_status_rejects_mode_change(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.pacing import PacingStatus

    manager = TaskManager(persistent_store=PersistentTaskStore(tmp_path / "task.json"))

    async def scenario():
        task_id = await manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "微博书").resolve()),
            pacing_mode="low_2_3_hours",
            keep_awake_when_plugged=False,
        )
        with pytest.raises(ValueError, match="档位"):
            await manager.update_pacing_status(task_id, PacingStatus(
                mode="low_4_6_hours",
                state="waiting",
                request_kind="detail",
                next_wait_seconds=1.0,
                target_min_seconds=14400,
                target_max_seconds=21600,
                disclaimer="目标区间不是完成时间承诺",
            ))

    asyncio.run(scenario())


def test_personal_archive_service_passes_required_pacing_fields_to_manager(tmp_path):
    from backend.app.schemas import ArchiveFolderInspection, PersonalArchiveRequest
    from backend.app.services.personal_archive_tasks import (
        PersonalArchiveTaskService,
        TaskStartInfo,
    )

    manager = MagicMock()
    manager.create_personal_archive = AsyncMock(return_value="0123456789ab")
    service = PersonalArchiveTaskService(
        manager=manager,
        inspector=lambda path, *, current_uid: ArchiveFolderInspection(
            state="empty",
            path=path,
        ),
    )
    service._launch = MagicMock(return_value=TaskStartInfo(
        task_id="0123456789ab",
        mode="create",
        self_uid="10001",
        self_screen_name="本人",
        worker=MagicMock(),
    ))
    request = PersonalArchiveRequest(
        output_dir=str((tmp_path / "微博书").resolve()),
        mode="create",
        pacing_mode="low_4_6_hours",
        keep_awake_when_plugged=True,
    )

    asyncio.run(service.start(request, {"uid": "10001", "screen_name": "本人"}))

    manager.create_personal_archive.assert_awaited_once_with(
        mode="create",
        # 所选目录不是档案时自动套一层「昵称_UID」子文件夹
        output_dir=str((tmp_path / "微博书").resolve() / "本人_10001"),
        expected_uid="10001",
        pacing_mode="low_4_6_hours",
        keep_awake_when_plugged=True,
        target_label=None,
    )


def test_late_pacing_status_cannot_recreate_terminal_persistent_record(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.pacing import PacingStatus

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)

    async def scenario():
        task_id = await manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "微博书").resolve()),
            pacing_mode="low_2_3_hours",
            keep_awake_when_plugged=False,
        )
        await manager.set_done(task_id, {"ok": True})
        await manager.update_pacing_status(task_id, PacingStatus(
            mode="low_2_3_hours",
            state="waiting",
            request_kind="detail",
            next_wait_seconds=10.0,
            target_min_seconds=7200,
            target_max_seconds=10800,
            disclaimer="目标区间不是完成时间承诺",
        ))

    asyncio.run(scenario())

    assert store.load() is None
