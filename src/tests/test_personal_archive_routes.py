"""本人微博书归档路由契约。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.main import app
from backend.app.schemas import (
    ArchiveFolderInspection,
    ArchiveInspectRequest,
    PersonalArchiveRequest,
)
from backend.app.services.task_manager import task_manager
from weibo_book.archive.sync import SyncResult
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.errors import OperationCancelled, WeiboError, WeiboErrorKind


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _identity() -> dict[str, str]:
    return {"uid": "10001", "screen_name": "本人"}


def _start_payload(output_dir: str, mode: str, **extra) -> dict:
    return {
        "output_dir": output_dir,
        "mode": mode,
        "pacing_mode": "standard",
        "keep_awake_when_plugged": False,
        **extra,
    }


def _inspection(
    state: str,
    path: Path,
    *,
    uid: str = "10001",
    total_posts: int = 0,
) -> ArchiveFolderInspection:
    return ArchiveFolderInspection(
        state=state,
        path=str(path),
        uid=uid,
        total_posts=total_posts,
    )


def _wait(task_id: str, timeout: float = 15.0) -> dict:
    # Windows CI 在发布门禁第二次全量测试的高负载下，后台归档可能超过 5 秒才进入终态；
    # 这里只放宽等待上限，不改变任务状态机或清理语义。
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = task_manager.snapshot(task_id)
        if snapshot and snapshot["state"] in {"done", "error", "cancelled"}:
            return snapshot
        time.sleep(0.02)
    snapshot = task_manager.snapshot(task_id)
    assert snapshot is not None
    return snapshot


def test_personal_request_models_only_expose_archive_fields():
    assert set(ArchiveInspectRequest.model_fields) == {"output_dir", "target_uid"}
    assert set(PersonalArchiveRequest.model_fields) == {
        "output_dir", "mode", "pacing_mode", "keep_awake_when_plugged", "target_uid",
    }
    for forbidden in (
        "url", "confirm_uid_mismatch", "post_ids",
        "max_posts", "start_date", "end_date", "formats",
    ):
        assert forbidden not in PersonalArchiveRequest.model_fields


def test_personal_request_forbids_old_cross_account_fields():
    with pytest.raises(ValidationError):
        PersonalArchiveRequest(
            output_dir="/tmp/archive",
            mode="create",
            pacing_mode="standard",
            keep_awake_when_plugged=False,
            confirm_uid_mismatch=True,
        )


def test_route_rejects_extra_fields_with_422(client, tmp_path):
    response = client.post(
        "/api/backup/start",
        json=_start_payload(str(tmp_path), "create", unknown_field="x"),
    )
    assert response.status_code == 422


def test_route_rejects_invalid_target_uid_with_400(client, tmp_path):
    response = client.post(
        "/api/backup/start",
        json=_start_payload(str(tmp_path), "create", target_uid="not-a-uid"),
    )
    assert response.status_code == 400
    assert "UID" in response.json()["detail"]


@pytest.mark.parametrize(
    ("state", "uid"),
    [
        ("empty", ""),
        ("archive", "10001"),
        ("ordinary_nonempty", ""),
        ("uid_mismatch", "other"),
        ("damaged", ""),
        ("legacy_index", ""),
    ],
)
def test_inspect_returns_read_only_folder_state(client, tmp_path, state, uid):
    expected = _inspection(state, tmp_path, uid=uid)
    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=expected,
         ) as inspect, \
         patch("backend.app.routers.router_backup.task_manager.create") as create:
        response = client.post(
            "/api/backup/inspect", json={"output_dir": str(tmp_path)}
        )

    assert response.status_code == 200
    assert response.json()["state"] == state
    inspect.assert_called_once_with(str(tmp_path), current_uid="10001")
    create.assert_not_called()


def test_inspect_uses_existing_auth_error_shape(client, tmp_path):
    error = WeiboError("未登录", kind=WeiboErrorKind.AUTH)
    with patch("backend.app.routers.router_backup.whoami", side_effect=error):
        response = client.post(
            "/api/backup/inspect", json={"output_dir": str(tmp_path)}
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "[认证]未登录"}


def test_legacy_index_requires_one_full_archive_build(tmp_path):
    (tmp_path / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}', encoding="utf-8"
    )

    from backend.app.services.archive_folder import inspect_archive_folder

    inspection = inspect_archive_folder(tmp_path, current_uid="10001")

    assert inspection.state == "legacy_index"
    assert inspection.message == "旧版备份目录，需要首次建立完整档案"


def test_legacy_create_moves_index_to_audit_after_full_success(client, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    legacy_payload = '{"uid":"10001","bids":["A"]}'
    (root / ".weishushu_index.json").write_text(legacy_payload, encoding="utf-8")
    (root / "旧版微博书.md").write_text("旧内容", encoding="utf-8")
    class FullArchiveSync:
        def __init__(self, *_args, sync_run_started, **_kwargs):
            self.sync_run_started = sync_run_started

        def run(self, mode):
            assert mode == "create"
            assert not root.exists()
            repository = ArchiveRepository.create(root, "10001", "本人")
            run_id = repository.begin_sync("create")
            self.sync_run_started(run_id)
            repository.finish_sync(run_id, "done", {"new_posts": 0})
            repository.update_manifest_success("2026-07-17T00:00:00+00:00")
            repository.close()
            return SyncResult("create", 0, 0, 0, 0, [])
    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             FullArchiveSync,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "create"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "done"
    audit = root / ".work" / "legacy" / ".weishushu_index.json"
    assert audit.read_text(encoding="utf-8") == legacy_payload
    assert not (root / "旧版微博书.md").exists()
    assert not list(tmp_path.glob(".legacy.legacy-*"))


def test_legacy_create_failure_restores_complete_old_directory(client, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}', encoding="utf-8"
    )
    (root / "旧版微博书.md").write_text("旧内容", encoding="utf-8")
    sync_instance = MagicMock()
    sync_instance.run.side_effect = WeiboError("模拟首次建立失败")

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "create"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "error"
    assert (root / ".weishushu_index.json").is_file()
    assert (root / "旧版微博书.md").read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".legacy.legacy-*"))


def test_legacy_create_constructor_failure_restores_old_directory(client, tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}', encoding="utf-8"
    )

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             side_effect=WeiboError("模拟同步器初始化失败"),
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "create"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "error"
    assert (root / ".weishushu_index.json").is_file()
    assert not list(tmp_path.glob(".legacy.legacy-*"))


@pytest.mark.asyncio
async def test_legacy_stage_cancellation_waits_and_restores_old_directory(
    tmp_path,
):
    from backend.app.routers.router_backup import _stage_legacy_safely
    from backend.app.services.backup_index import stage_legacy_archive

    root = tmp_path / "legacy"
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}', encoding="utf-8"
    )
    (root / "旧版微博书.md").write_text("旧内容", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    task_id = "0123456789ab"

    def slow_stage(output_dir, uid, received_task_id):
        assert received_task_id == task_id
        staged = stage_legacy_archive(output_dir, uid, received_task_id)
        started.set()
        assert release.wait(2)
        return staged

    with patch(
        "backend.app.routers.router_backup.stage_legacy_archive",
        side_effect=slow_stage,
    ):
        task = asyncio.create_task(
            _stage_legacy_safely(str(root), "10001", task_id)
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert (root / ".weishushu_index.json").is_file()
    assert (root / "旧版微博书.md").read_text(encoding="utf-8") == "旧内容"
    assert not list(tmp_path.glob(".legacy.legacy-*"))


def test_legacy_index_for_another_uid_is_not_rebuilt_as_current_account(
    client, tmp_path
):
    root = tmp_path / "legacy"
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        '{"uid":"20002","bids":["A"]}', encoding="utf-8"
    )
    sync_instance = MagicMock()

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "create"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "waiting_resume"
    persistent = task_manager.get(response.json()["task_id"]).persistent_record
    assert persistent.pause_reason == "authentication_required"
    assert (root / ".weishushu_index.json").is_file()
    sync_instance.run.assert_not_called()


def test_next_successful_incremental_cleans_legacy_audit(client, tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()
    audit = root / ".work" / "legacy"
    audit.mkdir(parents=True)
    (audit / ".weishushu_index.json").write_text("{}", encoding="utf-8")
    sync_instance = MagicMock()
    sync_instance.run.return_value = SyncResult("incremental", 0, 0, 0, 0, [])

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "incremental"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "done"
    assert not audit.exists()


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        ("create", "empty"),
        ("incremental", "archive"),
        ("rebuild", "archive"),
    ],
)
def test_start_accepts_only_valid_mode_folder_matrix(client, tmp_path, mode, state):
    result = SyncResult(mode, 0, 0, 0, 0, [])
    source = object()
    media_stager = object()
    sync_instance = MagicMock()
    sync_instance.run.return_value = result
    expected_root = tmp_path if state == "archive" else tmp_path / "本人_10001"

    def inspect_for_run(path, *, current_uid):
        # create 模式在同步完成后才应识别为微博书；用 run.called 代替调用
        # 计数，避免目录解析带来的只读检查次数变化影响用例。
        completed = mode == "create" and sync_instance.run.called
        return _inspection("archive" if completed else state, Path(path), uid=current_uid)

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             side_effect=inspect_for_run,
         ), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(source, media_stager),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ) as sync_class, \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), mode),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        snapshot = _wait(body["task_id"])

    assert body["mode"] == mode
    assert body["self_uid"] == "10001"
    assert body["self_screen_name"] == "本人"
    assert snapshot["state"] == "done"
    sync_class.assert_called_once()
    args, kwargs = sync_class.call_args
    assert args[0] == str(expected_root)
    assert args[1] is source
    assert args[2].whoami() == _identity()
    assert kwargs["media_stager"] is media_stager
    assert callable(kwargs["cancel_requested"])
    sync_instance.run.assert_called_once_with(mode)


@pytest.mark.parametrize(
    ("mode", "state", "message"),
    [
        ("create", "archive", "首次建立"),
        ("incremental", "empty", "现有微博书"),
        ("rebuild", "empty", "现有微博书"),
        ("create", "ordinary_nonempty", "非空目录"),
        ("incremental", "damaged", "已损坏"),
        ("rebuild", "legacy_index", "旧版索引"),
    ],
)
def test_start_rejects_invalid_mode_folder_matrix_before_task(
    client, tmp_path, mode, state, message
):
    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=_inspection(state, tmp_path),
         ), \
         patch("backend.app.routers.router_backup.task_manager.create") as create, \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies"
         ) as dependencies, \
         patch("backend.app.routers.router_backup.PersonalArchiveSync") as sync:
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), mode),
        )
    assert response.status_code == 409
    assert message in response.json()["detail"]
    create.assert_not_called()
    dependencies.assert_not_called()
    sync.assert_not_called()


def test_uid_mismatch_stops_before_source_task_and_sync(client, tmp_path):
    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=_inspection("uid_mismatch", tmp_path, uid="other"),
         ), \
         patch("backend.app.routers.router_backup.task_manager.create") as create, \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies"
         ) as dependencies, \
         patch("backend.app.routers.router_backup.PersonalArchiveSync") as sync:
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), "incremental"),
        )
    assert response.status_code == 409
    assert "其他账号" in response.json()["detail"]
    create.assert_not_called()
    dependencies.assert_not_called()
    sync.assert_not_called()


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"uid": 10001, "screen_name": "本人"},
        {"uid": "10001", "screen_name": 7},
        {"uid": "", "screen_name": "本人"},
        {"uid": "10001", "screen_name": " "},
    ],
)
def test_start_rejects_invalid_whoami_without_string_conversion(client, tmp_path, identity):
    with patch("backend.app.routers.router_backup.whoami", return_value=identity), \
         patch("backend.app.routers.router_backup.inspect_archive_folder") as inspect:
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), "create"),
        )
    assert response.status_code == 400
    assert "UID 和昵称必须是非空字符串" in response.json()["detail"]
    inspect.assert_not_called()


def test_default_source_factory_builds_real_adapters_without_network():
    from backend.app.routers.router_backup import build_personal_archive_dependencies
    from weibo_book.archive.source import ArchiveMediaStager, WeiboArchiveSource

    book = MagicMock()
    book.ensure_login.return_value = "SUB=stored"
    with patch("backend.app.routers.router_backup.WeiboBook", return_value=book), \
         patch("backend.app.routers.router_backup.WeiboExtractor") as extractor_class:
        source, stager = build_personal_archive_dependencies("10001")

    book.ensure_login.assert_called_once_with(force=False)
    extractor_class.assert_called_once_with(
        cookie_str="SUB=stored", image_quality=book.image_quality
    )
    assert isinstance(source, WeiboArchiveSource)
    assert source.extractor is extractor_class.return_value
    assert isinstance(stager, ArchiveMediaStager)


def test_registered_personal_archive_routes_are_unique():
    paths = [
        route.path
        for route in app.routes
        if route.path in {"/api/backup/inspect", "/api/backup/start"}
    ]
    assert paths.count("/api/backup/inspect") == 1
    assert paths.count("/api/backup/start") == 1


def test_recovery_endpoint_returns_sanitized_waiting_task_without_network(client, tmp_path):
    import asyncio

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="incremental",
            output_dir=str((tmp_path / "微博书").resolve()),
        )
    )
    record = task_manager.get(task_id)
    assert record is not None
    task_manager._persist(
        record,
        state="waiting_resume",
        pause_reason="unexpected_exit",
        saved_content="已保存 12 条微博",
    )
    record.state = "waiting_resume"

    response = client.get("/api/tasks/recovery")

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["task_id"] == task_id
    assert task["state"] == "waiting_resume"
    assert task["pause_reason"] == "unexpected_exit"
    assert task["saved_content"] == "已保存 12 条微博"
    assert "output_dir" not in task
    assert "archive_run_id" not in task


def test_pause_endpoint_uses_cooperative_persistent_transition(client, tmp_path):
    import asyncio

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="incremental",
            output_dir=str((tmp_path / "微博书").resolve()),
        )
    )

    response = client.post(f"/api/tasks/{task_id}/pause")

    assert response.status_code == 200
    assert response.json() == {"task_id": task_id, "state": "pausing"}
    assert task_manager.snapshot(task_id)["state"] == "pausing"

    repeated = client.post(f"/api/tasks/{task_id}/pause")
    assert repeated.status_code == 409


def test_persistent_action_endpoint_rejects_unknown_task_id(client):
    for action in ("pause", "resume", "abandon"):
        response = client.post(f"/api/tasks/0123456789ab/{action}")
        assert response.status_code == 404


def test_abandon_endpoint_converts_unexpected_cleanup_failure_to_chinese_error(
    client, tmp_path, monkeypatch,
):
    import asyncio
    from backend.app.routers import router_tasks

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "微博书").resolve()),
        )
    )
    record = task_manager.get(task_id)
    assert record is not None
    record.state = "waiting_resume"
    task_manager._persist(record, state="waiting_resume")
    monkeypatch.setattr(
        router_tasks,
        "_current_identity",
        lambda: {"uid": "10001", "screen_name": "测试用户"},
    )

    async def fail_abandon(_task_id, _identity):
        raise OSError("模拟清理失败")

    monkeypatch.setattr(router_tasks.personal_archive_tasks, "abandon", fail_abandon)

    response = client.post(f"/api/tasks/{task_id}/abandon")

    assert response.status_code == 500
    assert response.json() == {"detail": "放弃未完成部分失败，请查看日志"}


def test_lifespan_restores_interrupted_task_without_building_network_dependencies(
    tmp_path,
):
    from backend.app.services.persistent_task_store import PersistentTaskRecord

    store = task_manager._persistent_store
    assert store is not None
    store.save(PersistentTaskRecord(
        schema_version=7,
        task_id="0123456789ab",
        task_kind="personal_archive",
        mode="create",
        output_dir=str((tmp_path / "微博书").resolve()),
        state="running",
        phase="sync",
        archive_run_id=None,
        progress_current=1,
        progress_total=None,
        progress_unit="post",
        started_at="2026-07-17T00:00:00+00:00",
        saved_at="2026-07-17T00:00:01+00:00",
        pause_reason="",
        saved_content="已保存 1 条微博",
        expected_uid="10001",
        legacy_index_sha256=None,
        error_recoverable=False,
    ))

    with patch("backend.app.services.personal_archive_tasks.WeiboBook") as book, \
         TestClient(app) as isolated_client:
        response = isolated_client.get("/api/tasks/recovery")

    assert response.status_code == 200
    assert response.json()["task"]["state"] == "waiting_resume"
    assert response.json()["task"]["pause_reason"] == "unexpected_exit"
    book.assert_not_called()


@pytest.mark.parametrize("failure", ["digest_mismatch", "persist_digest"])
def test_lifespan_keeps_interrupted_legacy_cancel_recoverable_on_digest_error(
    tmp_path,
    monkeypatch,
    failure,
):
    from backend.app.services.backup_index import (
        stage_legacy_archive,
        staged_legacy_archive_sha256,
    )
    from backend.app.services.persistent_task_store import PersistentTaskStoreError

    store = task_manager._persistent_store
    assert store is not None
    root = (tmp_path / "微博书").resolve()
    root.mkdir()
    (root / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}',
        encoding="utf-8",
    )
    task_id = asyncio.run(task_manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
    ))
    staged = stage_legacy_archive(root, "10001", task_id)
    record = task_manager.get(task_id)
    assert record is not None
    digest = staged_legacy_archive_sha256(root, task_id)
    task_manager._persist(
        record,
        state="cancelling",
        legacy_index_sha256=("0" * 64 if failure == "digest_mismatch" else None),
    )
    task_manager._tasks.clear()
    if failure == "persist_digest":
        original_save = store.save
        failed_once = False

        def fail_first_digest_save(persistent):
            nonlocal failed_once
            if (
                not failed_once
                and persistent.state == "cancelling"
                and persistent.legacy_index_sha256 == digest
            ):
                failed_once = True
                raise PersistentTaskStoreError("模拟摘要持久化失败")
            original_save(persistent)

        monkeypatch.setattr(store, "save", fail_first_digest_save)

    with TestClient(app) as isolated_client:
        health = isolated_client.get("/healthz")
        recovery = isolated_client.get("/api/tasks/recovery")

    assert health.status_code == 200
    assert recovery.status_code == 200
    assert recovery.json()["task"]["state"] == "error"
    assert staged.is_dir()
    retained = store.load()
    assert retained is not None
    assert retained.state == "error"


def test_resume_endpoint_rejects_uid_mismatch_before_network(client, tmp_path):
    import asyncio

    from backend.app.routers import router_tasks

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="incremental",
            output_dir=str((tmp_path / "微博书").resolve()),
        )
    )
    record = task_manager.get(task_id)
    assert record is not None
    task_manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"
    dependencies = MagicMock()

    with patch.object(
        router_tasks.personal_archive_tasks,
        "inspector",
        return_value=_inspection("uid_mismatch", tmp_path, uid="20002"),
    ), patch.object(
        router_tasks.personal_archive_tasks,
        "dependency_builder",
        dependencies,
    ), patch(
        "backend.app.routers.router_tasks.whoami",
        return_value=_identity(),
    ):
        response = client.post(f"/api/tasks/{task_id}/resume")

    assert response.status_code in {401, 409}
    assert "账号" in response.json()["detail"]
    dependencies.assert_not_called()


def test_successful_backup_renders_fixed_archive_outputs(client, tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()
    sync_instance = MagicMock()
    sync_instance.run.return_value = SyncResult(
        "incremental", 137, 0, 0, 0, []
    )
    rendered = {
        "html": root / "微博书.html",
        "pdf": root / "微博书.pdf",
        "markdown": root / "微博书.md",
        "data": root / "data" / "archive-data.js",
    }

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=_inspection("archive", root, total_posts=137),
         ), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "weibo_book.archive.render_snapshot.ArchiveRenderer.render_all",
             return_value=rendered,
         ) as render_all:
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "incremental"),
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(response.json()["task_id"])

    assert snapshot["state"] == "done"
    assert snapshot["result"]["generated_files"] == [
        "微博书.html",
        "微博书.pdf",
        "微博书.md",
        "data/archive-data.js",
    ]
    assert snapshot["result"]["total_posts"] == 137
    assert snapshot["result"]["new_posts"] == 137
    assert snapshot["result"]["refreshed_posts"] == 0
    assert snapshot["result"]["changed_posts"] == 0
    render_all.assert_called_once()


def test_frontend_result_counts_use_explicit_archive_fields():
    from frontend_assets import frontend_bundle_asset

    source = frontend_bundle_asset().read_text(encoding="utf-8")

    for token in (
        "result.total_posts",
        "result.new_posts",
        "result.refreshed_posts",
        "result.changed_posts",
        "档案总数",
        "本次新增",
        "已刷新",
        "发生变化",
    ):
        assert token in source
    assert "result.posts_count" not in source


@pytest.mark.asyncio
async def test_route_defers_complete_phase_until_fixed_outputs_are_rendered():
    from backend.app.routers.router_backup import _ProgressReporter

    task_id = await task_manager.create()
    reporter = _ProgressReporter(task_id, asyncio.get_running_loop())
    await asyncio.to_thread(
        reporter.emit,
        {
            "phase": "complete",
            "pct": 1.0,
            "detail": "微博书归档已完成",
            "current": 50,
            "total": 50,
            "unit": "post",
        },
    )
    await reporter.drain()

    during_render = task_manager.snapshot(task_id)["progress_event"]
    assert during_render["phase"] == "generate"
    assert during_render["pct"] < 1.0

    await asyncio.to_thread(reporter.emit_render_complete)
    await reporter.drain()
    completed = task_manager.snapshot(task_id)["progress_event"]
    assert completed["phase"] == "complete"
    assert completed["pct"] == 1.0


def test_fixed_output_render_uses_the_same_archive_lock(tmp_path):
    from backend.app.routers.router_backup import render_personal_archive
    from weibo_book.archive.sync import _archive_lock

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()

    with _archive_lock(root), \
         patch(
             "backend.app.routers.router_backup.ArchiveRenderer.render_all"
         ) as render_all, \
         pytest.raises(WeiboError, match="正在备份"):
        render_personal_archive(str(root), "10001", lambda: False)

    render_all.assert_not_called()


def test_cancel_waits_for_render_worker_to_stop(client, tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()
    sync_instance = MagicMock()
    sync_instance.run.return_value = SyncResult(
        "incremental", 0, 50, 0, 0, []
    )
    started = threading.Event()
    stopped = threading.Event()

    def cancellable_render(
        _path, _uid, cancel_requested, _begin_commit, _pause_requested
    ):
        started.set()
        while not cancel_requested():
            time.sleep(0.01)
        stopped.set()
        raise OperationCancelled("任务已取消")

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=_inspection("archive", root),
         ), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             side_effect=cancellable_render,
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(root), "incremental"),
        )
        assert response.status_code == 200
        assert started.wait(2)
        cancelled = client.post(
            f"/api/tasks/{response.json()['task_id']}/cancel"
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelled"] is True
        assert stopped.wait(2)


# ====== v2.0.1 备份他人微博（target_uid） ======


class _FakeTargetInfo:
    uid = "20002"
    screen_name = "目标博主"


def _target_patches():
    """本人已登录（uid=10001），目标博主 20002 的身份解析全部打桩。"""
    extractor = MagicMock()
    extractor.get_user_info.return_value = _FakeTargetInfo()
    book = MagicMock()
    book.ensure_login.return_value = "SUB=fake-cookie"
    return (
        patch("backend.app.routers.router_backup.whoami", return_value=_identity()),
        patch("backend.app.routers.router_backup.WeiboBook", return_value=book),
        patch("backend.app.routers.router_backup.WeiboExtractor", return_value=extractor),
        extractor,
    )


def test_start_with_target_uid_uses_target_identity(client, tmp_path):
    whoami_patch, book_patch, extractor_patch, extractor = _target_patches()
    result = SyncResult("create", 0, 0, 0, 0, [])
    sync_instance = MagicMock()
    sync_instance.run.return_value = result

    def inspect_for_run(path, *, current_uid):
        completed = sync_instance.run.called
        return _inspection(
            "archive" if completed else "empty", Path(path), uid=current_uid
        )

    created_kwargs = {}
    original_create = task_manager.create_personal_archive

    async def spy_create(**kwargs):
        created_kwargs.update(kwargs)
        return await original_create(**kwargs)

    with whoami_patch, book_patch, extractor_patch, \
         patch.object(task_manager, "create_personal_archive", side_effect=spy_create), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             side_effect=inspect_for_run,
         ), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ) as sync_class, \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), "create", target_uid="20002"),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        snapshot = _wait(body["task_id"])

    extractor.get_user_info.assert_called_once_with("20002")
    assert body["self_uid"] == "20002"
    assert body["self_screen_name"] == "目标博主"
    assert snapshot["state"] == "done"
    # 他人任务持久记录带目标昵称，供恢复卡片区分本人/他人
    assert created_kwargs["target_label"] == "目标博主"
    args, _kwargs = sync_class.call_args
    assert args[0] == str(tmp_path / "目标博主_20002")
    assert args[2].whoami() == {"uid": "20002", "screen_name": "目标博主"}


def test_start_with_target_uid_equal_to_login_stays_self_mode(client, tmp_path):
    whoami_patch, book_patch, extractor_patch, extractor = _target_patches()
    with whoami_patch, book_patch, extractor_patch, \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             return_value=_inspection("ordinary_nonempty", tmp_path),
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), "create", target_uid="10001"),
        )
    # 目标即本人：不取目标资料，按本人模式走既有目录校验（非空目录 409）
    extractor.get_user_info.assert_not_called()
    assert response.status_code == 409
    assert "非空目录" in response.json()["detail"]


def test_start_self_mode_records_no_target_label(client, tmp_path):
    result = SyncResult("create", 0, 0, 0, 0, [])
    sync_instance = MagicMock()
    sync_instance.run.return_value = result

    def inspect_for_run(path, *, current_uid):
        completed = sync_instance.run.called
        return _inspection(
            "archive" if completed else "empty", Path(path), uid=current_uid
        )

    created_kwargs = {}
    original_create = task_manager.create_personal_archive

    async def spy_create(**kwargs):
        created_kwargs.update(kwargs)
        return await original_create(**kwargs)

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), \
         patch.object(task_manager, "create_personal_archive", side_effect=spy_create), \
         patch(
             "backend.app.routers.router_backup.inspect_archive_folder",
             side_effect=inspect_for_run,
         ), \
         patch(
             "backend.app.routers.router_backup.build_personal_archive_dependencies",
             return_value=(object(), object()),
         ), \
         patch(
             "backend.app.routers.router_backup.PersonalArchiveSync",
             return_value=sync_instance,
         ), \
         patch(
             "backend.app.routers.router_backup.render_personal_archive",
             return_value=[],
         ):
        response = client.post(
            "/api/backup/start",
            json=_start_payload(str(tmp_path), "create"),
        )
        assert response.status_code == 200, response.text
        _wait(response.json()["task_id"])

    assert created_kwargs["target_label"] is None


def test_inspect_with_target_uid_uses_target(client, tmp_path):
    whoami_patch, book_patch, extractor_patch, extractor = _target_patches()
    captured = {}

    def fake_inspect_selected(output_dir, uid, *, inspector):
        captured["uid"] = uid
        return _inspection("empty", Path(output_dir))

    with whoami_patch, book_patch, extractor_patch, \
         patch(
             "backend.app.routers.router_backup.inspect_selected_folder",
             side_effect=fake_inspect_selected,
         ):
        response = client.post(
            "/api/backup/inspect",
            json={"output_dir": str(tmp_path), "target_uid": "20002"},
        )
    assert response.status_code == 200, response.text
    assert captured["uid"] == "20002"


def test_archive_target_identity_reads_local_manifest(tmp_path):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    def inspector(path, *, current_uid):
        return ArchiveFolderInspection(
            state="archive", path=str(path), uid=current_uid,
            screen_name="目标博主",
        )

    service = PersonalArchiveTaskService(inspector=inspector)
    uid, screen_name = service._archive_target_identity(str(tmp_path), "20002")
    assert (uid, screen_name) == ("20002", "目标博主")

    def empty_name_inspector(path, *, current_uid):
        return ArchiveFolderInspection(
            state="archive", path=str(path), uid=current_uid, screen_name="",
        )

    service = PersonalArchiveTaskService(inspector=empty_name_inspector)
    with pytest.raises(WeiboError) as exc_info:
        service._archive_target_identity(str(tmp_path), "20002")
    assert exc_info.value.kind is WeiboErrorKind.AUTH


def test_resume_other_archive_passes_identity_gate(client, tmp_path):
    """他人归档续跑：登录账号与目标不一致时以本地档案身份为准。"""
    from unittest.mock import AsyncMock

    from backend.app.routers import router_tasks

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="incremental",
            output_dir=str((tmp_path / "目标博主_20002").resolve()),
            expected_uid="20002",
        )
    )
    record = task_manager.get(task_id)
    assert record is not None
    task_manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"

    def inspector(path, *, current_uid):
        return ArchiveFolderInspection(
            state="archive", path=str(path), uid="20002", screen_name="目标博主",
        )

    with patch.object(
        router_tasks.personal_archive_tasks, "inspector", side_effect=inspector
    ), patch(
        "backend.app.routers.router_tasks.whoami", return_value=_identity()
    ), patch.object(
        router_tasks.personal_archive_tasks.manager,
        "prepare_persistent_resume",
        new=AsyncMock(return_value=False),
    ):
        response = client.post(f"/api/tasks/{task_id}/resume")

    # 身份门禁已通过；在「任务状态不允许恢复」处停下，不再报账号不一致
    assert response.status_code == 409
    assert "当前任务状态不允许恢复" in response.json()["detail"]


def test_recovery_summary_exposes_target_label_for_other_archive(tmp_path):
    """他人归档的恢复摘要带目标昵称；本人归档为 None。"""
    other_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "目标博主_20002").resolve()),
            expected_uid="20002",
            target_label="目标博主",
        )
    )
    record = task_manager.get(other_id)
    assert record is not None
    task_manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"

    summary = task_manager.recovery_summary()
    assert summary is not None
    assert summary["target_label"] == "目标博主"

    task_manager._persistent_store.clear()
    task_manager._tasks.clear()

    self_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "本人_10001").resolve()),
            expected_uid="10001",
        )
    )
    record = task_manager.get(self_id)
    assert record is not None
    task_manager._persist(record, state="waiting_resume")
    record.state = "waiting_resume"

    summary = task_manager.recovery_summary()
    assert summary is not None
    assert summary["target_label"] is None


def test_archive_target_identity_falls_back_to_task_temporary(tmp_path):
    """create/rebuild 提交前正式目录不存在，身份确认回退到任务临时归档。"""
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    temporary = tmp_path / ".微博书.create-task-0123456789ab"
    temporary.mkdir()

    def inspector(path, *, current_uid):
        if "create-task" in str(path):
            return ArchiveFolderInspection(
                state="archive", path=str(path), uid=current_uid,
                screen_name="目标博主",
            )
        return _inspection("empty", Path(path))

    service = PersonalArchiveTaskService(inspector=inspector)
    uid, screen_name = service._archive_target_identity(
        str(tmp_path / "微博书"), "20002",
        mode="create", task_id="0123456789ab",
    )
    assert (uid, screen_name) == ("20002", "目标博主")


def test_archive_target_identity_zombie_create_reports_data_loss(tmp_path):
    """旧版本崩溃已删除临时归档：报「放弃后重新开始」而不是笼统的无法确认。"""
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    def inspector(path, *, current_uid):
        return _inspection("empty", Path(path))

    service = PersonalArchiveTaskService(inspector=inspector)
    with pytest.raises(WeiboError, match="放弃该任务后重新开始备份"):
        service._archive_target_identity(
            str(tmp_path / "微博书"), "20002",
            mode="create", task_id="0123456789ab",
        )


def test_abandon_tolerates_missing_task_temporary(tmp_path):
    """临时归档已被旧版本清理的僵尸记录仍可正常放弃。"""
    import uuid as uuid_module

    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService

    task_id = asyncio.run(
        task_manager.create_personal_archive(
            mode="create",
            output_dir=str((tmp_path / "测试博主_1000000001").resolve()),
            expected_uid="1000000001",
            target_label="测试博主",
        )
    )
    record = task_manager.get(task_id)
    assert record is not None
    asyncio.run(task_manager.set_archive_run_id(task_id, str(uuid_module.uuid4())))
    task_manager._persist(record, state="error", pause_reason="模拟旧版本崩溃")
    record.state = "error"

    service = PersonalArchiveTaskService(manager=task_manager)
    ok = asyncio.run(service.abandon(task_id, _identity()))

    assert ok is True
    assert task_manager._persistent_store.load() is None
    assert task_manager.snapshot(task_id)["state"] == "abandoned"
