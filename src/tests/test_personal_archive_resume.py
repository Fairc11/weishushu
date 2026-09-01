from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import uuid

import pytest

from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.discovery import ProfileItem, ProfilePage
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord
from weibo_book.archive.sync import PersonalArchiveSync
from weibo_book.errors import OperationPaused, WeiboError
from weibo_book.models import Post


TASK_ID = "0123456789ab"


class IdentityProvider:
    def whoami(self) -> dict[str, str]:
        return {"uid": "10001", "screen_name": "测试用户"}


class CountingSource:
    def __init__(self, uid: str = "10001") -> None:
        self.uid = uid
        self.fetch_calls: Counter[str] = Counter()

    def iter_profile_pages(self, uid: str):
        assert uid == self.uid
        yield ProfilePage(
            [ProfileItem("A"), ProfileItem("B")],
            is_last=True,
        )

    def fetch_post(self, uid: str, bid: str) -> Post:
        assert uid == self.uid
        self.fetch_calls[bid] += 1
        return Post(
            bid=bid,
            uid=uid,
            user_name="测试用户",
            user_avatar="",
            text=f"正文 {bid}",
            created_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

    def fetch_recent_comments(self, post_id: str, limit: int = 10):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "pause_reason"),
    [
        ("auth", "authentication_required"),
        ("exact_432", "rate_limited"),
    ],
)
async def test_personal_archive_worker_pauses_on_auth_and_exact_432(
    tmp_path,
    failure_kind,
    pause_reason,
):
    from crawl4weibo.exceptions.base import NetworkError

    from backend.app.schemas import ArchiveFolderInspection
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager
    from weibo_book.errors import WeiboErrorKind, classify_error

    root = (tmp_path / failure_kind).resolve()
    store = PersistentTaskStore(tmp_path / f"{failure_kind}.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    calls = 0

    def fail_dependency(_uid):
        nonlocal calls
        calls += 1
        if failure_kind == "auth":
            raise WeiboError(
                "登录状态已失效，请重新登录后继续",
                kind=WeiboErrorKind.AUTH,
            )
        exact = NetworkError("Encountered 432 anti-crawler block")
        raise WeiboError(
            "平台限制了当前请求频率",
            kind=classify_error(exact),
            original=exact,
        )

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=fail_dependency,
        inspector=lambda *_args, **_kwargs: ArchiveFolderInspection(
            state="empty",
            path=str(root),
        ),
    )
    started = service._launch(
        task_id,
        "create",
        str(root),
        "10001",
        "测试用户",
        resuming=False,
    )

    await started.worker

    assert calls == 1
    assert manager.snapshot(task_id)["state"] == "waiting_resume"
    assert store.load().pause_reason == pause_reason


def _write_legacy_archive(root, uid="10001"):
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        f'{{"uid":"{uid}","bids":["A"]}}',
        encoding="utf-8",
    )
    (root / "旧版微博书.md").write_text("旧内容", encoding="utf-8")


def test_legacy_stage_path_is_derived_from_exact_task_id(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive

    root = tmp_path / "微博书"
    _write_legacy_archive(root)

    staged = stage_legacy_archive(root, "10001", TASK_ID)

    assert staged == tmp_path / f".微博书.legacy-task-{TASK_ID}"
    assert staged.is_dir()
    assert not root.exists()


def test_legacy_stage_refuses_to_overwrite_exact_task_directory(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive

    root = tmp_path / "微博书"
    _write_legacy_archive(root)
    staged = tmp_path / f".微博书.legacy-task-{TASK_ID}"
    staged.mkdir()
    evidence = staged / "保留.txt"
    evidence.write_text("其他状态", encoding="utf-8")

    with pytest.raises(WeiboError, match="已存在"):
        stage_legacy_archive(root, "10001", TASK_ID)

    assert (root / ".weishushu_index.json").is_file()
    assert evidence.read_text(encoding="utf-8") == "其他状态"


def test_legacy_restore_refuses_stage_owned_by_another_task(tmp_path):
    from backend.app.services.backup_index import (
        restore_legacy_archive,
        stage_legacy_archive,
    )

    root = tmp_path / "微博书"
    _write_legacy_archive(root)
    staged = stage_legacy_archive(root, "10001", TASK_ID)

    with pytest.raises(WeiboError, match="暂存路径无效"):
        restore_legacy_archive(
            root,
            staged,
            "abcdef012345",
            "10001",
        )

    assert staged.is_dir()
    assert not root.exists()


def test_legacy_stage_refuses_intermediate_parent_symlink(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive

    require_symlink_capability(target_is_directory=True)
    physical_parent = tmp_path / "真实父目录"
    physical_parent.mkdir()
    linked_parent = tmp_path / "链接父目录"
    linked_parent.symlink_to(physical_parent, target_is_directory=True)
    root = linked_parent / "微博书"
    _write_legacy_archive(root)

    with pytest.raises(WeiboError, match="符号链接"):
        stage_legacy_archive(root, "10001", TASK_ID)

    assert (physical_parent / "微博书" / ".weishushu_index.json").is_file()


def test_legacy_stage_refuses_old_directory_symlink(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive

    require_symlink_capability(target_is_directory=True)
    physical_root = tmp_path / "真实旧目录"
    _write_legacy_archive(physical_root)
    linked_root = tmp_path / "微博书"
    linked_root.symlink_to(physical_root, target_is_directory=True)

    with pytest.raises(WeiboError, match="符号链接"):
        stage_legacy_archive(linked_root, "10001", TASK_ID)

    assert linked_root.is_symlink()
    assert (physical_root / ".weishushu_index.json").is_file()


def test_legacy_restore_refuses_staged_directory_symlink(tmp_path):
    from backend.app.services.backup_index import restore_legacy_archive

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "微博书"
    physical_stage = tmp_path / "真实暂存目录"
    _write_legacy_archive(physical_stage)
    staged = tmp_path / f".微博书.legacy-task-{TASK_ID}"
    staged.symlink_to(physical_stage, target_is_directory=True)

    with pytest.raises(WeiboError, match="符号链接"):
        restore_legacy_archive(root, staged, TASK_ID, "10001")

    assert staged.is_symlink()
    assert (physical_stage / ".weishushu_index.json").is_file()


async def _render_phase_legacy_task(tmp_path):
    from backend.app.services.backup_index import (
        stage_legacy_archive,
        staged_legacy_archive_sha256,
    )
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    _write_legacy_archive(root)
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    staged = stage_legacy_archive(root, "10001", task_id)
    await manager.set_legacy_index_sha256(
        task_id,
        staged_legacy_archive_sha256(root, task_id),
    )
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    run_id = repository.begin_sync("create")
    repository.finish_sync(run_id, "done", {"new_posts": 1})
    repository.update_manifest_success("2026-07-17T00:00:00+00:00")
    repository.close()
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        phase="render",
        archive_run_id=run_id,
    )
    record.state = "waiting_resume"
    return root, staged, store, manager, task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "cancel", "abandon"])
async def test_render_phase_rejects_same_uid_archive_from_different_sync_run(
    tmp_path,
    action,
):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    persisted_run_id = store.load().archive_run_id
    shutil.rmtree(root)
    replacement = ArchiveRepository.create(root, "10001", "测试用户")
    replacement_run_id = replacement.begin_sync("create")
    replacement.finish_sync(replacement_run_id, "done", {"new_posts": 0})
    replacement.update_manifest_success("2026-07-17T01:00:00+00:00")
    replacement.close()
    assert replacement_run_id != persisted_run_id
    service = PersonalArchiveTaskService(
        manager=manager,
        render_func=lambda *_args, **_kwargs: [],
    )

    with pytest.raises(WeiboError, match="同步记录"):
        if action == "resume":
            await service.resume(
                task_id,
                {"uid": "10001", "screen_name": "测试用户"},
            )
        elif action == "cancel":
            await service.cancel(task_id)
        else:
            await service.abandon(
                task_id,
                {"uid": "10001", "screen_name": "测试用户"},
            )

    assert staged.is_dir()
    assert (staged / "旧版微博书.md").read_text(encoding="utf-8") == "旧内容"
    assert not (root / ".work" / "legacy").exists()
    assert store.load() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_mode", "replacement_status", "message"),
    [
        ("rebuild", "done", "模式"),
        ("create", "running", "尚未完成"),
    ],
)
async def test_render_phase_requires_exact_completed_sync_mode(
    tmp_path,
    replacement_mode,
    replacement_status,
    message,
):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    shutil.rmtree(root)
    replacement = ArchiveRepository.create(root, "10001", "测试用户")
    replacement_run_id = replacement.begin_sync(replacement_mode)
    if replacement_status == "done":
        replacement.finish_sync(replacement_run_id, "done", {"new_posts": 0})
    replacement.update_manifest_success("2026-07-17T01:00:00+00:00")
    replacement.close()
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, archive_run_id=replacement_run_id)

    with pytest.raises(WeiboError, match=message):
        await PersonalArchiveTaskService(manager=manager).resume(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    assert staged.is_dir()
    assert store.load() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["audit_copied", "stage_deleted"])
@pytest.mark.parametrize("action", ["resume", "cancel", "abandon"])
async def test_legacy_finalize_retries_exact_crash_windows(
    tmp_path,
    monkeypatch,
    window,
    action,
):
    import backend.app.services.backup_index as backup_index
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None and persistent.archive_run_id is not None
    original_rmtree = backup_index.shutil.rmtree
    if window == "audit_copied":
        isolation = tmp_path / f".微博书.legacy-delete-task-{task_id}"

        def fail_after_audit(path):
            assert path == isolation
            raise OSError("模拟复制审计索引后退出")

        monkeypatch.setattr(backup_index.shutil, "rmtree", fail_after_audit)
        with pytest.raises(OSError, match="复制审计索引后退出"):
            backup_index.finalize_legacy_archive(
                root,
                staged,
                task_id,
                "10001",
                persistent.archive_run_id,
                "create",
                persistent.legacy_index_sha256,
            )
        monkeypatch.setattr(backup_index.shutil, "rmtree", original_rmtree)
        assert not staged.exists()
        assert isolation.is_dir()
    else:
        backup_index.finalize_legacy_archive(
            root,
            staged,
            task_id,
            "10001",
            persistent.archive_run_id,
            "create",
            persistent.legacy_index_sha256,
        )
        assert not staged.exists()

    service = PersonalArchiveTaskService(
        manager=manager,
        render_func=lambda *_args, **_kwargs: [],
    )
    if action == "resume":
        started = await service.resume(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )
        await started.worker
        assert manager.snapshot(task_id)["state"] == "done"
    elif action == "cancel":
        assert await service.cancel(task_id)
    else:
        assert await service.abandon(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    audit = root / ".work" / "legacy"
    assert (audit / ".weishushu_index.json").is_file()
    assert (audit / ".weishushu-legacy-finalize.json").is_file()
    assert not staged.exists()
    assert store.load() is None


@pytest.mark.asyncio
async def test_legacy_finalize_refuses_unrelated_existing_audit_index(tmp_path):
    import backend.app.services.backup_index as backup_index

    root, staged, store, _manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None and persistent.archive_run_id is not None
    audit = root / ".work" / "legacy"
    audit.mkdir(parents=True)
    unrelated = audit / ".weishushu_index.json"
    unrelated.write_text('{"uid":"10001","bids":["OTHER"]}', encoding="utf-8")

    with pytest.raises(WeiboError, match="审计内容"):
        backup_index.finalize_legacy_archive(
            root,
            staged,
            task_id,
            "10001",
            persistent.archive_run_id,
            "create",
            persistent.legacy_index_sha256,
        )

    assert staged.is_dir()
    assert unrelated.read_text(encoding="utf-8") == '{"uid":"10001","bids":["OTHER"]}'


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["marker", "index_and_marker", "unrelated_item"])
async def test_deleted_legacy_stage_rejects_tampered_audit_and_preserves_evidence(
    tmp_path,
    tamper,
):
    import backend.app.services.backup_index as backup_index
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None
    assert persistent.archive_run_id is not None
    assert persistent.legacy_index_sha256 is not None
    backup_index.finalize_legacy_archive(
        root,
        staged,
        task_id,
        "10001",
        persistent.archive_run_id,
        "create",
        persistent.legacy_index_sha256,
    )
    audit = root / ".work" / "legacy"
    marker = audit / ".weishushu-legacy-finalize.json"
    if tamper == "marker":
        marker.write_text("{}", encoding="utf-8")
    elif tamper == "index_and_marker":
        replacement = b'{"uid":"10001","bids":["REPLACED"]}'
        (audit / ".weishushu_index.json").write_bytes(replacement)
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        marker_payload["index_sha256"] = hashlib.sha256(replacement).hexdigest()
        marker.write_text(
            json.dumps(marker_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    else:
        (audit / "无关证据.txt").write_text("保留", encoding="utf-8")

    with pytest.raises(WeiboError, match="旧索引审计"):
        await PersonalArchiveTaskService(manager=manager).cancel(task_id)

    assert not staged.exists()
    assert audit.is_dir()
    assert store.load() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incremental", "rebuild"])
async def test_prior_legacy_audit_does_not_block_later_render_recovery(tmp_path, mode):
    import backend.app.services.backup_index as backup_index
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root, staged, old_store, _old_manager, old_task_id = await _render_phase_legacy_task(
        tmp_path
    )
    old_persistent = old_store.load()
    assert old_persistent is not None
    assert old_persistent.archive_run_id is not None
    assert old_persistent.legacy_index_sha256 is not None
    backup_index.finalize_legacy_archive(
        root,
        staged,
        old_task_id,
        "10001",
        old_persistent.archive_run_id,
        "create",
        old_persistent.legacy_index_sha256,
    )
    audit = root / ".work" / "legacy"
    assert audit.is_dir()

    repository = ArchiveRepository.open(root, "10001")
    run_id = repository.begin_sync(mode)
    repository.finish_sync(run_id, "done", {"new_posts": 0})
    repository.update_manifest_success("2026-07-17T02:00:00+00:00")
    repository.close()
    store = PersistentTaskStore(tmp_path / f"{mode}-task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode=mode,
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        phase="render",
        archive_run_id=run_id,
    )
    record.state = "waiting_resume"
    service = PersonalArchiveTaskService(
        manager=manager,
        render_func=lambda *_args, **_kwargs: [],
    )

    started = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )
    await started.worker

    assert manager.snapshot(task_id)["state"] == "done"
    assert not audit.exists()


@pytest.mark.asyncio
async def test_legacy_finalize_rejects_source_replacement_before_stage_deletion(
    tmp_path,
    monkeypatch,
):
    import backend.app.services.backup_index as backup_index

    root, staged, store, _manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None
    assert persistent.archive_run_id is not None
    assert persistent.legacy_index_sha256 is not None
    original_write_marker = backup_index._write_finalize_marker

    def replace_source_after_marker(path, payload):
        original_write_marker(path, payload)
        replacement = staged / ".replacement-index.json"
        replacement.write_text(
            '{"uid":"10001","bids":["REPLACED"]}',
            encoding="utf-8",
        )
        os.replace(replacement, staged / ".weishushu_index.json")

    monkeypatch.setattr(
        backup_index,
        "_write_finalize_marker",
        replace_source_after_marker,
    )

    with pytest.raises(WeiboError, match="收尾时已变化"):
        backup_index.finalize_legacy_archive(
            root,
            staged,
            task_id,
            "10001",
            persistent.archive_run_id,
            "create",
            persistent.legacy_index_sha256,
        )

    assert staged.is_dir()
    assert (root / ".work" / "legacy" / ".weishushu_index.json").is_file()


@pytest.mark.asyncio
async def test_legacy_finalize_isolates_exact_stage_before_recursive_deletion(
    tmp_path,
    monkeypatch,
):
    import backend.app.services.backup_index as backup_index

    root, staged, store, _manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None
    assert persistent.archive_run_id is not None
    assert persistent.legacy_index_sha256 is not None
    original_validate = backup_index._validate_finalize_audit
    displaced = tmp_path / "原暂存证据"
    swapped = False

    def replace_stage_after_final_validation(*args, **kwargs):
        nonlocal swapped
        result = original_validate(*args, **kwargs)
        if result and not swapped:
            swapped = True
            os.replace(staged, displaced)
            staged.mkdir()
            (staged / "攻击者证据.txt").write_text("不得删除", encoding="utf-8")
        return result

    monkeypatch.setattr(
        backup_index,
        "_validate_finalize_audit",
        replace_stage_after_final_validation,
    )

    with pytest.raises(WeiboError, match="身份"):
        backup_index.finalize_legacy_archive(
            root,
            staged,
            task_id,
            "10001",
            persistent.archive_run_id,
            "create",
            persistent.legacy_index_sha256,
        )

    isolation = tmp_path / f".微博书.legacy-delete-task-{task_id}"
    assert displaced.is_dir()
    assert (displaced / ".weishushu_index.json").is_file()
    assert isolation.is_dir()
    assert (isolation / "攻击者证据.txt").read_text(encoding="utf-8") == "不得删除"


@pytest.mark.asyncio
async def test_legacy_finalize_refuses_existing_delete_isolation_path(tmp_path):
    import backend.app.services.backup_index as backup_index

    root, staged, store, _manager, task_id = await _render_phase_legacy_task(tmp_path)
    persistent = store.load()
    assert persistent is not None
    assert persistent.archive_run_id is not None
    assert persistent.legacy_index_sha256 is not None
    isolation = tmp_path / f".微博书.legacy-delete-task-{task_id}"
    isolation.mkdir()
    evidence = isolation / "保留.txt"
    evidence.write_text("已有证据", encoding="utf-8")

    with pytest.raises(WeiboError, match="删除隔离"):
        backup_index.finalize_legacy_archive(
            root,
            staged,
            task_id,
            "10001",
            persistent.archive_run_id,
            "create",
            persistent.legacy_index_sha256,
        )

    assert staged.is_dir()
    assert evidence.read_text(encoding="utf-8") == "已有证据"


@pytest.mark.asyncio
async def test_schema_one_incremental_uid_is_saved_only_after_local_match(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "正确账号")
    run_id = repository.begin_sync("incremental")
    repository.finish_sync(run_id, "paused", {"new_posts": 0})
    repository.close()
    path = tmp_path / "task.json"
    store = PersistentTaskStore(path)
    creating = TaskManager(store)
    task_id = await creating.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
    )
    record = creating.get(task_id)
    assert record is not None
    creating._persist(record, state="waiting_resume", archive_run_id=run_id)
    payload = asdict(store.load())
    payload["schema_version"] = 1
    payload.pop("expected_uid")
    payload.pop("legacy_index_sha256")
    payload.pop("error_recoverable")
    for field in (
        "pacing_mode", "keep_awake_when_plugged", "pacing_state",
        "pacing_request_kind", "next_wait_seconds", "checkpoint",
        "target_label",
    ):
        payload.pop(field)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manager = TaskManager(store)
    migrated = store.load()
    assert migrated is not None and migrated.expected_uid is None
    assert await manager.restore_waiting_record(migrated)
    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (_ for _ in ()).throw(
            OperationPaused("任务已暂停")
        ),
    )

    # v2.0.1 他人归档语义：登录账号与归档目标不一致时，以本地档案记录的
    # 目标账号为准续跑，expected_uid 记录为本地档案 UID 而非登录 UID。
    started = await service.resume(
        task_id,
        {"uid": "20002", "screen_name": "其他登录账号"},
    )
    await started.worker
    assert store.load().expected_uid == "10001"

    started = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "正确账号"},
    )
    await started.worker
    assert store.load().expected_uid == "10001"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "incremental"])
async def test_schema_one_abandon_saves_uid_only_after_exact_local_match(
    tmp_path,
    mode,
):
    from backend.app.services.backup_index import stage_legacy_archive
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    store = PersistentTaskStore(tmp_path / "task.json")
    creating = TaskManager(store)
    if mode == "create":
        _write_legacy_archive(root)
        task_id = await creating.create_personal_archive(
            mode="create",
            output_dir=str(root),
        )
        staged = stage_legacy_archive(root, "10001", task_id)
        run_id = None
    else:
        repository = ArchiveRepository.create(root, "10001", "正确账号")
        run_id = repository.begin_sync("incremental")
        repository.finish_sync(run_id, "paused", {"new_posts": 0})
        repository.close()
        task_id = await creating.create_personal_archive(
            mode="incremental",
            output_dir=str(root),
        )
        staged = None
    record = creating.get(task_id)
    assert record is not None
    creating._persist(
        record,
        state="waiting_resume",
        archive_run_id=run_id,
    )
    payload = asdict(store.load())
    payload["schema_version"] = 1
    payload.pop("expected_uid")
    payload.pop("legacy_index_sha256")
    payload.pop("error_recoverable")
    for field in (
            "pacing_mode", "keep_awake_when_plugged", "pacing_state",
            "pacing_request_kind", "next_wait_seconds", "checkpoint",
            "target_label",
    ):
        payload.pop(field)
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manager = TaskManager(store)
    migrated = store.load()
    assert migrated is not None and migrated.expected_uid is None
    assert await manager.restore_waiting_record(migrated)
    service = PersonalArchiveTaskService(manager=manager)

    # v2.0.1 他人归档语义：放弃清理只操作本地文件，以本地档案记录的
    # 目标账号为准，不要求当前登录账号与之一致。
    assert await service.abandon(
        task_id,
        {"uid": "20002", "screen_name": "其他登录账号"},
    )
    assert store.load() is None
    if staged is not None:
        assert not staged.exists()
        assert (root / ".weishushu_index.json").is_file()


@pytest.mark.asyncio
async def test_render_phase_resume_finalizes_exact_legacy_stage(tmp_path):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, _store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    render_calls = []
    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (_ for _ in ()).throw(
            AssertionError("渲染恢复不得重新建立网络依赖")
        ),
        render_func=lambda output_dir, *_args, **_kwargs: (
            render_calls.append(output_dir) or []
        ),
    )

    started = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )
    await started.worker

    assert manager.snapshot(task_id)["state"] == "done"
    assert render_calls == [str(root)]
    assert not staged.exists()
    assert (root / ".work" / "legacy" / ".weishushu_index.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "abandon"])
async def test_render_phase_terminal_action_finalizes_legacy_without_deleting_archive(
    tmp_path,
    action,
):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    root, staged, store, manager, task_id = await _render_phase_legacy_task(tmp_path)
    service = PersonalArchiveTaskService(manager=manager)

    if action == "cancel":
        assert await service.cancel(task_id)
    else:
        assert await service.abandon(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    archive = ArchiveRepository.open(root, "10001")
    assert archive.get_latest_sync_status("create")[1] == "done"
    archive.close()
    assert not staged.exists()
    assert (root / ".work" / "legacy" / ".weishushu_index.json").is_file()
    assert store.load() is None


@pytest.mark.asyncio
async def test_cancel_rejects_staged_legacy_uid_different_from_trusted_task_uid(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    _write_legacy_archive(root, uid="20002")
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    staged = stage_legacy_archive(root, "20002", task_id)
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"

    with pytest.raises(WeiboError, match="账号"):
        await PersonalArchiveTaskService(manager=manager).cancel(task_id)

    assert staged.is_dir()
    assert not root.exists()
    assert store.load() is not None


@pytest.mark.asyncio
async def test_force_exit_after_legacy_stage_can_resume_from_exact_task_path(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    _write_legacy_archive(root)
    store = PersistentTaskStore(tmp_path / "task.json")
    first_manager = TaskManager(store)
    task_id = await first_manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    staged = stage_legacy_archive(root, "10001", task_id)
    assert store.load().archive_run_id is None

    persistent = store.reconcile_after_process_start()
    assert persistent is not None and persistent.state == "waiting_resume"
    resumed_manager = TaskManager(store)
    assert await resumed_manager.restore_waiting_record(persistent)
    source = CountingSource()
    service = PersonalArchiveTaskService(
        manager=resumed_manager,
        dependency_builder=lambda _uid: (source, None),
        render_func=lambda *_args, **_kwargs: [],
    )

    started = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )
    await started.worker

    assert resumed_manager.snapshot(task_id)["state"] == "done"
    assert root.is_dir()
    assert not staged.exists()
    assert (root / ".work" / "legacy" / ".weishushu_index.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "abandon"])
async def test_force_exit_after_legacy_stage_can_restore_on_local_terminal_action(
    tmp_path,
    action,
):
    from backend.app.services.backup_index import stage_legacy_archive
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    _write_legacy_archive(root)
    store = PersistentTaskStore(tmp_path / "task.json")
    first_manager = TaskManager(store)
    task_id = await first_manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    staged = stage_legacy_archive(root, "10001", task_id)
    persistent = store.reconcile_after_process_start()
    assert persistent is not None
    resumed_manager = TaskManager(store)
    assert await resumed_manager.restore_waiting_record(persistent)
    service = PersonalArchiveTaskService(manager=resumed_manager)

    if action == "cancel":
        assert await service.cancel(task_id)
    else:
        assert await service.abandon(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    assert root.is_dir()
    assert not staged.exists()
    assert (root / ".weishushu_index.json").is_file()
    assert (root / "旧版微博书.md").read_text(encoding="utf-8") == "旧内容"
    assert store.load() is None


@pytest.mark.asyncio
async def test_legacy_resume_reads_true_index_uid_before_processing(tmp_path):
    from backend.app.services.backup_index import stage_legacy_archive
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    _write_legacy_archive(root, uid="20002")
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(mode="create", output_dir=str(root))
    staged = stage_legacy_archive(root, "20002", task_id)
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"
    service = PersonalArchiveTaskService(manager=manager)

    with pytest.raises(WeiboError, match="账号"):
        await service.resume(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    assert staged.is_dir()
    assert not root.exists()
    assert store.load() is not None
    assert store.load().expected_uid is None

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (CountingSource(uid="20002"), None),
        render_func=lambda *_args, **_kwargs: [],
    )
    started = await service.resume(
        task_id,
        {"uid": "20002", "screen_name": "正确账号"},
    )
    await started.worker

    assert manager.snapshot(task_id)["state"] == "done"
    assert not staged.exists()


def test_create_resumes_task_archive_without_refetching_committed_post(tmp_path):
    root = tmp_path / "微博书"
    temporary = tmp_path / f".微博书.create-task-{TASK_ID}"
    source = CountingSource()
    run_ids: list[str] = []

    def pause_after_first_commit() -> bool:
        if not temporary.is_dir():
            return False
        repository = ArchiveRepository.open(temporary, "10001")
        unfinished = repository.get_unfinished_sync("create")
        repository.close()
        return (
            unfinished is not None
            and unfinished.checkpoint.get("completed_bids") == ["A"]
        )

    first = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        task_id=TASK_ID,
        pause_requested=pause_after_first_commit,
        sync_run_started=run_ids.append,
    )
    with pytest.raises(OperationPaused, match="任务已暂停"):
        first.run("create")

    assert temporary.is_dir()
    assert str(uuid.UUID(run_ids[0])) == run_ids[0]
    paused = ArchiveRepository.open(temporary, "10001")
    assert paused.get_post("A") is not None
    unfinished = paused.get_unfinished_sync("create")
    assert unfinished is not None
    assert unfinished.checkpoint["completed_bids"] == ["A"]
    paused.close()
    assert not root.exists()

    resumed = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        task_id=TASK_ID,
        pause_requested=lambda: False,
    )
    result = resumed.run("create")

    assert result.new_posts == 2
    assert source.fetch_calls == Counter({"A": 1, "B": 1})
    assert not temporary.exists()
    archive = ArchiveRepository.open(root, "10001")
    assert archive.get_post("A") is not None
    assert archive.get_post("B") is not None
    archive.close()


def test_rebuild_keeps_formal_archive_until_resumed_task_finishes(tmp_path):
    root = tmp_path / "微博书"
    formal = ArchiveRepository.create(root, "10001", "测试用户")
    formal.upsert_post(PostRecord(bid="OLD", uid="10001", text="旧正文"))
    formal.close()
    temporary = tmp_path / f".微博书.rebuild-task-{TASK_ID}"
    source = CountingSource()

    def pause_after_first_commit() -> bool:
        if not temporary.is_dir():
            return False
        repository = ArchiveRepository.open(temporary, "10001")
        unfinished = repository.get_unfinished_sync("rebuild")
        repository.close()
        return (
            unfinished is not None
            and unfinished.checkpoint.get("completed_bids") == ["A"]
        )

    with pytest.raises(OperationPaused, match="任务已暂停"):
        PersonalArchiveSync(
            root,
            source,
            IdentityProvider(),
            task_id=TASK_ID,
            pause_requested=pause_after_first_commit,
        ).run("rebuild")

    unchanged = ArchiveRepository.open(root, "10001")
    assert unchanged.get_post("OLD") is not None
    assert unchanged.get_post("A") is None
    unchanged.close()

    result = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        task_id=TASK_ID,
    ).run("rebuild")

    assert result.new_posts == 2
    assert source.fetch_calls == Counter({"A": 1, "B": 1})
    replaced = ArchiveRepository.open(root, "10001")
    assert replaced.get_post("OLD") is None
    assert replaced.get_post("A") is not None
    assert replaced.get_post("B") is not None
    replaced.close()


class PagedSource(CountingSource):
    def __init__(self) -> None:
        super().__init__()
        self.requested_pages: list[int] = []

    def iter_profile_pages(
        self,
        uid: str,
        *,
        start_page: int = 1,
        pin_orders: dict[str, int] | None = None,
        next_pin_order: int = 1,
    ):
        assert uid == "10001"
        self.requested_pages.append(start_page)
        pages = {
            1: ProfilePage([ProfileItem("A", True, 1)], is_last=False),
            2: ProfilePage([ProfileItem("B")], is_last=True),
        }
        for page_number in range(start_page, 3):
            yield pages[page_number]


def test_create_resumes_profile_discovery_from_next_saved_page(tmp_path):
    root = tmp_path / "微博书"
    temporary = tmp_path / f".微博书.create-task-{TASK_ID}"
    source = PagedSource()

    def pause_after_first_page() -> bool:
        if not temporary.is_dir():
            return False
        repository = ArchiveRepository.open(temporary, "10001")
        unfinished = repository.get_unfinished_sync("create")
        repository.close()
        return (
            unfinished is not None
            and unfinished.checkpoint.get("next_profile_page") == 2
        )

    with pytest.raises(OperationPaused, match="任务已暂停"):
        PersonalArchiveSync(
            root,
            source,
            IdentityProvider(),
            task_id=TASK_ID,
            pause_requested=pause_after_first_page,
        ).run("create")

    result = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        task_id=TASK_ID,
    ).run("create")

    assert result.new_posts == 2
    assert source.requested_pages == [1, 2]
    assert source.fetch_calls == Counter({"A": 1, "B": 1})


@pytest.mark.asyncio
async def test_service_resume_from_render_phase_does_not_repeat_sync(tmp_path):
    from backend.app.schemas import ArchiveFolderInspection
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, phase="render", state="waiting_resume")
    record.state = "waiting_resume"
    sync_calls: list[str] = []
    render_calls: list[str] = []

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (object(), object()),
        sync_factory=lambda *_args, **_kwargs: sync_calls.append("sync"),
        render_func=lambda output_dir, *_args, **_kwargs: (
            render_calls.append(output_dir)
            or ["微博书.html", "微博书.pdf", "微博书.md", "data/archive-data.js"]
        ),
        inspector=lambda path, *, current_uid: ArchiveFolderInspection(
            state="archive",
            path=path,
            uid=current_uid,
            total_posts=2,
        ),
    )

    info = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )
    await info.worker

    assert sync_calls == []
    assert render_calls == [str(root)]
    assert manager.snapshot(task_id)["state"] == "done"


@pytest.mark.asyncio
async def test_service_recognizes_completed_create_before_phase_was_saved(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    repository.upsert_post(PostRecord(bid="A", uid="10001", text="正文"))
    completed_run = repository.begin_sync("create")
    repository.finish_sync(completed_run, "done", {"new_posts": 1})
    repository.update_manifest_success("2026-07-17T00:00:00+00:00")
    repository.close()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, state="waiting_resume", phase="sync")
    record.state = "waiting_resume"
    render_calls: list[str] = []

    def unexpected_network(_uid):
        raise AssertionError("同步已完成时不应重复建立网络依赖")

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=unexpected_network,
        render_func=lambda output_dir, *_args, **_kwargs: (
            render_calls.append(output_dir)
            or ["微博书.html", "微博书.pdf", "微博书.md", "data/archive-data.js"]
        ),
    )

    info = await service.resume(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )
    await info.worker

    assert render_calls == [str(root)]
    assert manager.snapshot(task_id)["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "incremental", "rebuild"])
async def test_abandon_failure_before_sync_keeps_verified_formal_directory(tmp_path, mode):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    if mode == "create":
        root.mkdir()
    else:
        repository = ArchiveRepository.create(root, "10001", "测试用户")
        repository.close()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode=mode,
        output_dir=str(root),
        expected_uid="10001",
    )
    await manager.set_error(task_id, "任务执行失败，请查看日志后重试")

    assert await PersonalArchiveTaskService(manager=manager).abandon(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )

    assert root.is_dir()
    if mode == "create":
        assert list(root.iterdir()) == []
    else:
        assert (root / "manifest.json").is_file()
        assert (root / "data" / "archive.db").is_file()
    assert store.load() is None
    assert manager.recovery_summary() is None


@pytest.mark.asyncio
async def test_abandon_create_failure_before_sync_refuses_nonempty_directory(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    root.mkdir()
    (root / "用户文件.txt").write_text("保留", encoding="utf-8")
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    await manager.set_error(task_id, "任务执行失败，请查看日志后重试")

    with pytest.raises(WeiboError, match="精确同步记录标识"):
        await PersonalArchiveTaskService(manager=manager).abandon(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    assert (root / "用户文件.txt").read_text(encoding="utf-8") == "保留"
    assert store.load() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "rebuild"])
async def test_abandon_replacement_removes_only_exact_task_archive(tmp_path, mode):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    if mode == "create":
        root.mkdir()
        preserved = {"empty": True}
    else:
        formal = ArchiveRepository.create(root, "10001", "测试用户")
        formal.upsert_post(PostRecord(bid="OLD", uid="10001", text="旧正文"))
        formal.close()
        preserved = {}
        for name in ("微博书.html", "微博书.pdf", "微博书.md"):
            (root / name).write_bytes(f"旧-{name}".encode())
            preserved[name] = (root / name).read_bytes()
        data = root / "data" / "archive-data.js"
        data.write_bytes(b"old-data")
        preserved["data/archive-data.js"] = data.read_bytes()

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(mode=mode, output_dir=str(root))
    temporary = root.parent / f".{root.name}.{mode}-task-{task_id}"
    temporary_repo = ArchiveRepository.create(temporary, "10001", "测试用户")
    run_id = temporary_repo.begin_sync(mode)
    temporary_repo.update_sync_checkpoint(run_id, {"completed_bids": ["A"]})
    temporary_repo.finish_sync(run_id, "paused", {"new_posts": 1})
    temporary_repo.close()
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        archive_run_id=run_id,
    )
    record.state = "waiting_resume"

    release_worker = asyncio.Event()
    record._asyncio_task = asyncio.create_task(release_worker.wait())

    abandon_task = asyncio.create_task(PersonalArchiveTaskService(manager=manager).abandon(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    ))
    await asyncio.sleep(0)
    assert not abandon_task.done(), "放弃清理前必须等待工作协程完全退出"
    release_worker.set()

    abandoned = await abandon_task

    assert abandoned is True
    assert not temporary.exists()
    assert store.load() is None
    if mode == "create":
        assert root.is_dir() and not any(root.iterdir())
    else:
        formal = ArchiveRepository.open(root, "10001")
        assert formal.get_post("OLD") is not None
        formal.close()
        for name, content in preserved.items():
            assert (root / name).read_bytes() == content


@pytest.mark.asyncio
async def test_abandon_incremental_clears_exact_checkpoint_but_keeps_commits(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    repository.upsert_post(PostRecord(bid="A", uid="10001", text="已提交正文"))
    run_id = repository.begin_sync("incremental")
    repository.update_sync_checkpoint(run_id, {"completed_bids": ["A"]})
    repository.finish_sync(run_id, "paused", {"new_posts": 1})
    work = root / ".work" / run_id
    work.mkdir(parents=True)
    repository.close()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        archive_run_id=run_id,
    )
    record.state = "waiting_resume"

    assert await PersonalArchiveTaskService(manager=manager).abandon(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    )

    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_post("A") is not None
    assert repository.get_sync_run(run_id).status == "abandoned"
    assert repository.get_sync_run(run_id).checkpoint == {}
    assert repository._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    repository.close()
    assert not work.exists()
    assert store.load() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["auth", "exact_432"])
async def test_accepted_cancel_cleans_local_state_when_worker_hits_platform_error(
    tmp_path,
    failure_kind,
):
    from crawl4weibo.exceptions.base import NetworkError

    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager, run_in_background
    from weibo_book.errors import WeiboErrorKind, classify_error

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    repository.upsert_post(PostRecord(bid="A", uid="10001", text="已提交正文"))
    run_id = repository.begin_sync("incremental")
    repository.update_sync_checkpoint(run_id, {"completed_bids": ["A"]})
    work = root / ".work" / run_id
    work.mkdir(parents=True)
    (work / "pending.bin").write_bytes(b"pending")
    repository.close()

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    await manager.set_archive_run_id(task_id, run_id)
    assert await manager.request_cancel(task_id)
    service = PersonalArchiveTaskService(manager=manager)

    async def fail_after_cancel():
        if failure_kind == "auth":
            raise WeiboError("登录状态已失效", kind=WeiboErrorKind.AUTH)
        exact = NetworkError("Encountered 432 anti-crawler block")
        raise WeiboError(
            "平台限制了当前请求频率",
            kind=classify_error(exact),
            original=exact,
        )

    await run_in_background(
        task_id,
        fail_after_cancel,
        manager=manager,
        persistent_cancel_handler=service.finish_accepted_cancel,
    )

    assert manager.snapshot(task_id)["state"] == "cancelled"
    assert store.load() is None
    assert manager.recovery_summary() is None
    assert not work.exists()
    checked = ArchiveRepository.open(root, "10001")
    assert checked.get_sync_run(run_id).status == "cancelled"
    assert checked.get_sync_run(run_id).checkpoint == {}
    checked.close()
    next_task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    assert next_task_id != task_id
    await manager.set_cancelled(next_task_id)
    for handle in manager._gc_timers.values():
        handle.cancel()


@pytest.mark.asyncio
async def test_accepted_cancel_wins_after_pause_signal_was_already_created(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager, run_in_background

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    run_id = repository.begin_sync("incremental")
    repository.update_sync_checkpoint(run_id, {"completed_bids": ["A"]})
    work = root / ".work" / run_id
    work.mkdir(parents=True)
    (work / "pending.bin").write_bytes(b"pending")
    repository.close()

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    await manager.set_archive_run_id(task_id, run_id)
    service = PersonalArchiveTaskService(manager=manager)
    pause_signal_created = asyncio.Event()
    release_signal = asyncio.Event()

    async def deliver_created_pause_signal():
        pause_signal = OperationPaused("任务已暂停")
        pause_signal_created.set()
        await release_signal.wait()
        raise pause_signal

    worker = asyncio.create_task(run_in_background(
        task_id,
        deliver_created_pause_signal,
        manager=manager,
        persistent_cancel_handler=service.finish_accepted_cancel,
    ))
    await pause_signal_created.wait()
    assert await manager.request_cancel(task_id)
    release_signal.set()
    await worker

    assert manager.snapshot(task_id)["state"] == "cancelled"
    assert store.load() is None
    assert manager.recovery_summary() is None
    assert not work.exists()
    checked = ArchiveRepository.open(root, "10001")
    assert checked.get_sync_run(run_id).status == "cancelled"
    assert checked.get_sync_run(run_id).checkpoint == {}
    checked.close()
    next_task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    assert next_task_id != task_id
    await manager.set_cancelled(next_task_id)
    for handle in manager._gc_timers.values():
        handle.cancel()


@pytest.mark.asyncio
async def test_abandon_waits_for_paused_worker_to_exit_before_cleanup(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str((tmp_path / "微博书").resolve()),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        archive_run_id="00000000-0000-0000-0000-000000000001",
    )
    record.state = "waiting_resume"
    release_worker = asyncio.Event()
    record._asyncio_task = asyncio.create_task(release_worker.wait())
    cleanup_called = asyncio.Event()
    service = PersonalArchiveTaskService(manager=manager)
    service._cleanup_local_state = lambda *_args: cleanup_called.set()

    abandon_task = asyncio.create_task(service.abandon(
        task_id,
        {"uid": "10001", "screen_name": "测试用户"},
    ))
    await asyncio.sleep(0.05)

    assert not cleanup_called.is_set()
    release_worker.set()
    assert await abandon_task is True
    assert cleanup_called.is_set()


@pytest.mark.asyncio
async def test_cancel_waiting_incremental_finishes_local_cleanup_without_worker(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    run_id = repository.begin_sync("incremental")
    repository.update_sync_checkpoint(run_id, {"completed_bids": ["A"]})
    repository.finish_sync(run_id, "paused", {"new_posts": 1})
    repository.close()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="incremental",
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        archive_run_id=run_id,
    )
    record.state = "waiting_resume"

    assert await PersonalArchiveTaskService(manager=manager).cancel(task_id)

    assert manager.snapshot(task_id)["state"] == "cancelled"
    assert store.load() is None
    repository = ArchiveRepository.open(root, "10001")
    sync = repository.get_sync_run(run_id)
    assert sync.status == "cancelled"
    assert sync.checkpoint == {}
    repository.close()


@pytest.mark.asyncio
async def test_abandon_refuses_task_archive_symlink_and_preserves_target(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    require_symlink_capability(target_is_directory=True)
    root = (tmp_path / "微博书").resolve()
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "keep.txt"
    evidence.write_text("保留证据", encoding="utf-8")
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    temporary = root.parent / f".{root.name}.create-task-{task_id}"
    temporary.symlink_to(outside, target_is_directory=True)
    record = manager.get(task_id)
    assert record is not None
    manager._persist(
        record,
        state="waiting_resume",
        archive_run_id="00000000-0000-4000-8000-000000000000",
    )
    record.state = "waiting_resume"

    with pytest.raises(WeiboError, match="安全目录"):
        await PersonalArchiveTaskService(manager=manager).abandon(
            task_id,
            {"uid": "10001", "screen_name": "测试用户"},
        )

    assert evidence.read_text(encoding="utf-8") == "保留证据"
    assert temporary.is_symlink()
    assert store.load() is not None


@pytest.mark.asyncio
async def test_persistent_progress_defers_complete_until_render_finishes(tmp_path):
    """抓取结束不等于任务完成：渲染期间必须停留在 generate 阶段。"""
    import threading
    import time

    from backend.app.schemas import ArchiveFolderInspection, PersonalArchiveRequest
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.sync import SyncResult

    root = (tmp_path / "微博书").resolve()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)
    inspection_calls: list[str] = []

    def inspector(path, *, current_uid):
        inspection_calls.append(path)
        if len(inspection_calls) <= 2:
            return ArchiveFolderInspection(state="empty", path=path)
        return ArchiveFolderInspection(
            state="archive",
            path=path,
            uid=current_uid,
            total_posts=2,
        )

    def sync_factory(*_args, **kwargs):
        callback = kwargs["progress_callback"]

        class _Sync:
            def run(self, mode):
                callback({
                    "phase": "extract",
                    "pct": 0.5,
                    "detail": "已提取 1/2 条微博",
                    "current": 1,
                    "total": 2,
                    "unit": "post",
                })
                callback({
                    "phase": "complete",
                    "pct": 1.0,
                    "detail": "微博书归档已完成",
                    "current": 2,
                    "total": 2,
                    "unit": "post",
                })
                return SyncResult(
                    mode=mode,
                    new_posts=2,
                    refreshed_posts=0,
                    changed_posts=0,
                    unavailable_posts=0,
                    generated_files=[],
                )

        return _Sync()

    holder: dict[str, str] = {}
    observed: dict[str, dict] = {}

    def render_func(output_dir, *_args, **_kwargs):
        deadline = time.monotonic() + 5
        while "task_id" not in holder and time.monotonic() < deadline:
            time.sleep(0.01)
        snapshot = manager.snapshot(holder["task_id"])
        observed["during_render"] = dict(snapshot["progress_event"])
        return ["微博书.html", "微博书.pdf", "微博书.md", "data/archive-data.js"]

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (object(), object()),
        sync_factory=sync_factory,
        render_func=render_func,
        inspector=inspector,
    )

    info = await service.start(
        PersonalArchiveRequest(
            output_dir=str(root),
            mode="create",
            pacing_mode="standard",
            keep_awake_when_plugged=False,
        ),
        {"uid": "10001", "screen_name": "测试用户"},
    )
    holder["task_id"] = info.task_id
    await info.worker

    during_render = observed["during_render"]
    assert during_render["phase"] == "generate"
    assert during_render["pct"] < 1.0
    assert "正在生成" in during_render["detail"]
    assert during_render["elapsed_seconds"] >= 0

    final = manager.snapshot(info.task_id)
    assert final["state"] == "done"
    assert final["progress_event"]["phase"] == "complete"
    assert final["progress_event"]["pct"] == 1.0
    assert final["progress_event"]["detail"] == "微博书归档与固定文件已完成"
    assert final["progress_event"]["elapsed_seconds"] >= 0


@pytest.mark.asyncio
async def test_persistent_progress_render_resume_emits_generate_before_render(tmp_path):
    """从 render 阶段恢复时没有 sync 事件，也要先广播 generate 阶段。"""
    from backend.app.schemas import ArchiveFolderInspection
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager

    root = (tmp_path / "微博书").resolve()
    store = PersistentTaskStore(tmp_path / "task.json")
    manager = TaskManager(persistent_store=store)
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    )
    record = manager.get(task_id)
    assert record is not None
    manager._persist(record, phase="render", state="waiting_resume")
    record.state = "waiting_resume"
    observed: dict[str, dict] = {}

    def render_func(output_dir, *_args, **_kwargs):
        observed["during_render"] = dict(manager.snapshot(task_id)["progress_event"])
        return ["微博书.html", "微博书.pdf", "微博书.md", "data/archive-data.js"]

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (object(), object()),
        sync_factory=lambda *_args, **_kwargs: pytest.fail("不应重复同步"),
        render_func=render_func,
        inspector=lambda path, *, current_uid: ArchiveFolderInspection(
            state="archive",
            path=path,
            uid=current_uid,
            total_posts=2,
        ),
    )

    info = await service.resume(task_id, {"uid": "10001", "screen_name": "测试用户"})
    await info.worker

    assert observed["during_render"]["phase"] == "generate"
    assert observed["during_render"]["pct"] == pytest.approx(0.96)
    assert observed["during_render"]["elapsed_seconds"] >= 0
    final = manager.snapshot(task_id)
    assert final["state"] == "done"
    assert final["progress_event"]["phase"] == "complete"
