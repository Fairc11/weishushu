"""阶段 1 单任务持久记录的原子性与安全边界。"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from tests.symlink_capability import require_symlink_capability


def _record(tmp_path: Path, **changes):
    from backend.app.services.persistent_task_store import PersistentTaskRecord

    values = {
        "schema_version": 7,
        "task_id": "0123456789ab",
        "task_kind": "personal_archive",
        "mode": "create",
        "output_dir": str((tmp_path / "微博书").resolve()),
        "state": "running",
        "phase": "sync",
        "archive_run_id": None,
        "progress_current": 0,
        "progress_total": None,
        "progress_unit": "post",
        "started_at": "2026-07-17T00:00:00+00:00",
        "saved_at": "2026-07-17T00:00:00+00:00",
        "pause_reason": "",
        "saved_content": "尚未提交微博",
        "expected_uid": "10001",
        "legacy_index_sha256": None,
        "error_recoverable": False,
        "pacing_mode": "standard",
        "keep_awake_when_plugged": False,
        "pacing_state": "standard",
        "pacing_request_kind": None,
        "next_wait_seconds": None,
        "checkpoint": {},
        "target_label": None,
    }
    values.update(changes)
    return PersistentTaskRecord(**values)


def _drop_pacing_fields(payload: dict) -> None:
    for field in (
        "pacing_mode",
        "keep_awake_when_plugged",
        "pacing_state",
        "pacing_request_kind",
        "next_wait_seconds",
    ):
        payload.pop(field)


def test_store_round_trips_only_the_exact_schema_with_private_permissions(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskRecord,
        PersistentTaskStore,
    )

    path = tmp_path / "state" / "task.json"
    store = PersistentTaskStore(path)
    record = _record(tmp_path)

    store.save(record)

    assert store.load() == record
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == set(PersistentTaskRecord.__dataclass_fields__)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_store_returns_none(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    assert PersistentTaskStore(tmp_path / "missing.json").load() is None


def test_store_round_trips_target_label_for_other_blogger(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "state" / "task.json"
    store = PersistentTaskStore(path)
    record = _record(tmp_path, target_label="郭德纲")

    store.save(record)

    assert store.load() == record
    assert store.load().target_label == "郭德纲"


def test_schema_one_record_loads_with_unknown_trusted_uid(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "task.json"
    payload = _record(tmp_path).__dict__.copy()
    payload["schema_version"] = 1
    payload.pop("checkpoint")
    payload.pop("expected_uid")
    payload.pop("legacy_index_sha256")
    payload.pop("error_recoverable")
    payload.pop("target_label")
    _drop_pacing_fields(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = PersistentTaskStore(path).load()

    assert loaded is not None
    assert loaded.schema_version == 7
    assert loaded.expected_uid is None
    assert loaded.legacy_index_sha256 is None
    assert loaded.error_recoverable is False
    assert loaded.target_label is None


def test_schema_two_record_loads_with_unknown_legacy_index_sha256(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "task.json"
    payload = _record(tmp_path).__dict__.copy()
    payload["schema_version"] = 2
    payload.pop("checkpoint")
    payload.pop("legacy_index_sha256")
    payload.pop("error_recoverable")
    payload.pop("target_label")
    _drop_pacing_fields(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = PersistentTaskStore(path).load()

    assert loaded is not None
    assert loaded.schema_version == 7
    assert loaded.expected_uid == "10001"
    assert loaded.legacy_index_sha256 is None
    assert loaded.error_recoverable is False


def test_schema_three_error_migrates_with_previous_manual_resume_semantics(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "task.json"
    payload = _record(tmp_path, state="error").__dict__.copy()
    payload["schema_version"] = 3
    payload.pop("checkpoint")
    payload.pop("error_recoverable")
    payload.pop("target_label")
    _drop_pacing_fields(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = PersistentTaskStore(path).load()

    assert loaded is not None
    assert loaded.schema_version == 7
    assert loaded.error_recoverable is True


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("state", ["waiting_resume", "error"])
def test_legacy_schema_load_rewrites_current_record_atomically(
    tmp_path,
    schema_version,
    state,
):
    from backend.app.services.persistent_task_store import (
        PersistentTaskRecord,
        PersistentTaskStore,
    )

    path = tmp_path / "task.json"
    payload = _record(
        tmp_path,
        state=state,
        pause_reason="unexpected_exit" if state == "waiting_resume" else "归档校验失败",
    ).__dict__.copy()
    payload["schema_version"] = schema_version
    if schema_version < 6:
        payload.pop("checkpoint")
    payload.pop("target_label")
    if schema_version < 5:
        _drop_pacing_fields(payload)
    if schema_version < 4:
        payload.pop("error_recoverable")
    if schema_version == 1:
        payload.pop("expected_uid")
        payload.pop("legacy_index_sha256")
    elif schema_version == 2:
        payload.pop("legacy_index_sha256")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = PersistentTaskStore(path).load()
    rewritten = json.loads(path.read_text(encoding="utf-8"))

    assert loaded is not None
    assert rewritten["schema_version"] == 7
    assert rewritten["target_label"] is None
    assert set(rewritten) == set(PersistentTaskRecord.__dataclass_fields__)
    assert rewritten["state"] == state
    expected_recoverable = state == "error" if schema_version < 4 else False
    assert rewritten["error_recoverable"] is expected_recoverable


@pytest.mark.parametrize("invalid_version", [True, 1.0])
def test_schema_one_migration_rejects_non_integer_version(tmp_path, invalid_version):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    path = tmp_path / "task.json"
    payload = _record(tmp_path).__dict__.copy()
    payload["schema_version"] = invalid_version
    payload.pop("checkpoint")
    payload.pop("expected_uid")
    payload.pop("legacy_index_sha256")
    payload.pop("error_recoverable")
    payload.pop("target_label")
    _drop_pacing_fields(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PersistentTaskStoreError, match="持久任务记录"):
        PersistentTaskStore(path).load()


def test_clear_is_idempotent_and_removes_only_the_record(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "state" / "task.json"
    sibling = path.parent / "keep.txt"
    store = PersistentTaskStore(path)
    store.save(_record(tmp_path))
    sibling.write_text("保留", encoding="utf-8")

    store.clear()
    store.clear()

    assert store.load() is None
    assert sibling.read_text(encoding="utf-8") == "保留"


def test_clear_refuses_symlink_and_preserves_target(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    require_symlink_capability(target_is_directory=False)
    target = tmp_path / "outside.txt"
    target.write_text("不得删除", encoding="utf-8")
    path = tmp_path / "task.json"
    path.symlink_to(target)

    with pytest.raises(PersistentTaskStoreError, match="符号链接"):
        PersistentTaskStore(path).clear()
    assert target.read_text(encoding="utf-8") == "不得删除"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 8),
        ("schema_version", True),
        ("task_id", "ABCDEF012345"),
        ("task_kind", "other"),
        ("mode", "unknown"),
        ("mode", []),
        ("output_dir", "relative/path"),
        ("state", "unknown"),
        ("state", []),
        ("phase", "unknown"),
        ("phase", []),
        ("archive_run_id", "not-a-uuid"),
        ("progress_current", -1),
        ("progress_current", True),
        ("progress_total", -1),
        ("progress_unit", ""),
        ("started_at", "2026-07-17"),
        ("saved_at", "not-a-time"),
        ("pause_reason", 3),
        ("saved_content", None),
        ("error_recoverable", "true"),
        ("target_label", ""),
        ("target_label", "   "),
        ("target_label", 7),
        ("target_label", "x" * 65),
    ],
)
def test_store_rejects_invalid_record_values(tmp_path, field, value):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    path = tmp_path / "task.json"
    payload = _record(tmp_path).__dict__ | {field: value}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PersistentTaskStoreError, match="持久任务记录"):
        PersistentTaskStore(path).load()


def test_store_rejects_extra_or_missing_fields(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    path = tmp_path / "task.json"
    payload = _record(tmp_path).__dict__.copy()
    payload["unexpected"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PersistentTaskStoreError, match="字段"):
        PersistentTaskStore(path).load()

    payload.pop("unexpected")
    payload.pop("mode")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PersistentTaskStoreError, match="字段"):
        PersistentTaskStore(path).load()


def test_store_rejects_symlink_without_reading_target(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    require_symlink_capability(target_is_directory=False)
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(_record(tmp_path).__dict__), encoding="utf-8")
    path = tmp_path / "task.json"
    path.symlink_to(target)

    with pytest.raises(PersistentTaskStoreError, match="符号链接"):
        PersistentTaskStore(path).load()


def test_store_save_refuses_symlink_and_preserves_target(tmp_path):
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    require_symlink_capability(target_is_directory=False)
    target = tmp_path / "outside.txt"
    target.write_text("不得修改", encoding="utf-8")
    path = tmp_path / "task.json"
    path.symlink_to(target)

    with pytest.raises(PersistentTaskStoreError, match="符号链接"):
        PersistentTaskStore(path).save(_record(tmp_path))
    assert target.read_text(encoding="utf-8") == "不得修改"


def test_atomic_replace_failure_preserves_previous_record(tmp_path, monkeypatch):
    import backend.app.services.persistent_task_store as store_module
    from backend.app.services.persistent_task_store import (
        PersistentTaskStore,
        PersistentTaskStoreError,
    )

    path = tmp_path / "task.json"
    store = PersistentTaskStore(path)
    original = _record(tmp_path)
    store.save(original)

    def fail_replace(source, destination):
        raise OSError("模拟原子替换失败")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(PersistentTaskStoreError, match="保存持久任务记录失败"):
        store.save(replace(original, progress_current=1))

    assert store.load() == original
    assert not list(path.parent.glob(".task.json.*.tmp"))


def test_store_saves_private_record_without_posix_descriptor_operations(tmp_path, monkeypatch):
    import backend.app.services.persistent_task_store as store_module
    from backend.app.services.persistent_task_store import PersistentTaskStore

    path = tmp_path / "state" / "task.json"
    parent = path.parent
    original_open = store_module.os.open

    monkeypatch.delattr(store_module.os, "fchmod", raising=False)
    monkeypatch.setattr(store_module, "_IS_WINDOWS", True, raising=False)

    def reject_directory_open(target, flags, *args, **kwargs):
        if Path(target) == parent:
            raise AssertionError("Windows 不应为目录调用 POSIX fsync")
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "open", reject_directory_open)

    store = PersistentTaskStore(path)
    record = _record(tmp_path)
    store.save(record)

    assert store.load() == record
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("state", ["running", "pausing"])
def test_reconcile_after_process_start_marks_interrupted_work_waiting(tmp_path, state):
    from backend.app.services.persistent_task_store import PersistentTaskStore

    store = PersistentTaskStore(tmp_path / "task.json")
    store.save(_record(tmp_path, state=state))

    reconciled = store.reconcile_after_process_start()

    assert reconciled is not None
    assert reconciled.state == "waiting_resume"
    assert reconciled.pause_reason == "unexpected_exit"
    assert store.load() == reconciled
