from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from backend.app.schemas import ArchiveFolderInspection
from backend.app.services import archive_folder as archive_folder_module
from backend.app.services.archive_folder import (
    archive_folder_name,
    inspect_archive_folder,
    inspect_selected_folder,
    resolve_archive_dir,
)
from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord
from weibo_book.errors import WeiboError


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, int, str], ...]:
    if not root.exists() and not root.is_symlink():
        return ()
    root_stat = root.lstat()
    entries = (
        [root, *sorted(root.rglob("*"))]
        if stat.S_ISDIR(root_stat.st_mode)
        else [root]
    )
    snapshot = []
    for entry in entries:
        entry_stat = entry.lstat()
        digest = ""
        if stat.S_ISLNK(entry_stat.st_mode):
            kind = "symlink"
            digest = hashlib.sha256(entry.readlink().as_posix().encode()).hexdigest()
        elif stat.S_ISDIR(entry_stat.st_mode):
            kind = "dir"
        else:
            kind = "file"
        if stat.S_ISREG(entry_stat.st_mode):
            digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        relative = "." if entry == root else entry.relative_to(root).as_posix()
        snapshot.append(
            (relative, kind, entry_stat.st_size, entry_stat.st_mtime_ns, digest)
        )
    return tuple(snapshot)


def _create_archive(root: Path, *, uid: str = "10001") -> None:
    repository = ArchiveRepository.create(root, uid=uid, screen_name="测试用户")
    repository.upsert_post(PostRecord(bid="A", uid=uid, text="正文"))
    repository.close()


def _inspect_without_writes(path: Path, current_uid: str) -> ArchiveFolderInspection:
    before = _tree_snapshot(path)
    result = inspect_archive_folder(path, current_uid=current_uid)
    after = _tree_snapshot(path)
    assert after == before
    return result


def test_inspection_model_has_exact_defaults():
    inspection = ArchiveFolderInspection(state="empty", path="/tmp/archive")

    assert inspection.model_dump() == {
        "state": "empty",
        "path": "/tmp/archive",
        "uid": "",
        "screen_name": "",
        "total_posts": 0,
        "last_successful_sync_at": "",
        "message": "",
    }


def test_nonexistent_path_is_empty_and_is_not_created(tmp_path):
    selected = tmp_path / "new-archive"

    inspection = _inspect_without_writes(selected, current_uid="10001")

    assert inspection.state == "empty"
    assert inspection.path == str(selected)
    assert not selected.exists()


def test_existing_empty_directory_is_empty(tmp_path):
    selected = tmp_path / "empty"
    selected.mkdir()

    assert _inspect_without_writes(selected, "10001").state == "empty"


def test_empty_directory_stays_empty_without_directory_fd_support(tmp_path, monkeypatch):
    selected = tmp_path / "empty"
    selected.mkdir()
    monkeypatch.setattr(
        archive_folder_module,
        "_SUPPORTS_DIRECTORY_FDS",
        False,
    )

    assert _inspect_without_writes(selected, "10001").state == "empty"


def test_ordinary_nonempty_directory_is_not_modified(tmp_path):
    selected = tmp_path / "ordinary"
    selected.mkdir()
    (selected / "照片.jpg").write_bytes(b"image")

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "ordinary_nonempty"
    assert "普通非空文件夹" in inspection.message


def test_file_path_uses_existing_repository_nonempty_semantics(tmp_path):
    selected = tmp_path / "not-a-directory.txt"
    selected.write_text("内容", encoding="utf-8")

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "ordinary_nonempty"
    assert "不是文件夹" in inspection.message


def test_valid_archive_returns_identity_count_and_last_sync(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected)
    manifest_path = selected / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["last_successful_sync_at"] = "2026-07-14T01:02:03+00:00"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "archive"
    assert inspection.uid == "10001"
    assert inspection.screen_name == "测试用户"
    assert inspection.total_posts == 1
    assert inspection.last_successful_sync_at == "2026-07-14T01:02:03+00:00"


def test_schema_one_archive_is_recognized_without_migrating_it(tmp_path):
    selected = tmp_path / "archive"
    repository = ArchiveRepository.create(selected, uid="10001", screen_name="测试用户")
    repository.upsert_post(PostRecord(bid="A", uid="10001", text="正文"))
    for table in (
        "following_state",
        "following_changes",
        "following_names",
        "following_relationships",
        "following_objects",
        "following_snapshot_items",
        "following_snapshots",
    ):
        repository._connection.execute(f"DROP TABLE {table}")
    repository._connection.execute(
        "UPDATE archive_meta SET value_json = '1' WHERE key = 'schema_version'"
    )
    repository.close()
    manifest_path = selected / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "archive"
    assert inspection.total_posts == 1


def test_inspection_uses_read_only_path_snapshot_without_directory_fd_support(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    monkeypatch.setattr(
        archive_folder_module,
        "_SUPPORTS_DIRECTORY_FDS",
        False,
    )

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "archive"
    assert inspection.uid == "10001"
    assert inspection.total_posts == 1


def test_valid_archive_with_other_uid_returns_uid_mismatch(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected, uid="20002")

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "uid_mismatch"
    assert inspection.uid == "20002"
    assert inspection.screen_name == "测试用户"
    assert inspection.total_posts == 1
    assert "其他账号" in inspection.message


@pytest.mark.parametrize("missing", ["manifest", "database"])
def test_half_archive_is_damaged(tmp_path, missing):
    selected = tmp_path / "archive"
    _create_archive(selected)
    target = (
        selected / "manifest.json"
        if missing == "manifest"
        else selected / "data" / "archive.db"
    )
    target.unlink()

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "归档" in inspection.message
    assert any("\u4e00" <= character <= "\u9fff" for character in inspection.message)


def test_broken_manifest_is_damaged_with_chinese_message(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected)
    (selected / "manifest.json").write_text("{broken", encoding="utf-8")

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "归档清单损坏" in inspection.message


def test_manifest_with_wrong_structure_is_damaged(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected)
    (selected / "manifest.json").write_text(
        '{"schema_version":1,"uid":"10001"}', encoding="utf-8"
    )

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "归档清单损坏" in inspection.message


def test_database_missing_required_table_is_damaged(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected)
    connection = sqlite3.connect(selected / "data" / "archive.db")
    connection.execute("DROP TABLE media")
    connection.commit()
    connection.close()

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "归档数据库损坏" in inspection.message


def test_integrity_failure_is_damaged_and_connection_is_closed(tmp_path, monkeypatch):
    selected = tmp_path / "archive"
    _create_archive(selected)
    real_connect = archive_folder_module._connect_read_only
    connections = []

    class IntegrityFailureConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.closed = False

        def execute(self, sql, parameters=()):
            if sql == "PRAGMA integrity_check":
                return type("Rows", (), {"fetchone": lambda self: ("broken",)})()
            return self.wrapped.execute(sql, parameters)

        def close(self):
            self.closed = True
            self.wrapped.close()

    def connect(path):
        connection = IntegrityFailureConnection(real_connect(path))
        connections.append(connection)
        return connection

    monkeypatch.setattr(archive_folder_module, "_connect_read_only", connect)

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "完整性检查未通过" in inspection.message
    assert len(connections) == 1
    assert connections[0].closed is True


def test_successful_inspection_closes_connection(tmp_path, monkeypatch):
    selected = tmp_path / "archive"
    _create_archive(selected)
    real_connect = archive_folder_module._connect_read_only
    connections = []

    class TrackedConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.closed = False

        def execute(self, sql, parameters=()):
            return self.wrapped.execute(sql, parameters)

        def close(self):
            self.closed = True
            self.wrapped.close()

    def connect(path):
        connection = TrackedConnection(real_connect(path))
        connections.append(connection)
        return connection

    monkeypatch.setattr(archive_folder_module, "_connect_read_only", connect)

    assert inspect_archive_folder(selected, "10001").state == "archive"
    assert len(connections) == 1
    assert connections[0].closed is True


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_read_only_connection_closes_when_pragma_initialization_fails(
    tmp_path, monkeypatch, fail_on_call
):
    database_path = tmp_path / "archive.db"
    database_path.write_bytes(b"")

    class FailingConnection:
        def __init__(self):
            self.execute_calls = 0
            self.closed = False

        def execute(self, sql):
            self.execute_calls += 1
            if self.execute_calls == fail_on_call:
                raise RuntimeError("模拟 PRAGMA 初始化失败")

        def close(self):
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(
        archive_folder_module.sqlite3,
        "connect",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(RuntimeError, match="PRAGMA 初始化失败"):
        archive_folder_module._connect_read_only(database_path)

    assert connection.closed is True


def test_exact_legacy_index_marker_requires_full_archive_build(tmp_path):
    selected = tmp_path / "legacy"
    selected.mkdir()
    (selected / ".weishushu_index.json").write_text(
        '{"uid":"10001","bids":["A"]}', encoding="utf-8"
    )

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "legacy_index"
    assert inspection.message == "旧版备份目录，需要首次建立完整档案"


def test_archive_markers_take_precedence_over_legacy_index(tmp_path):
    selected = tmp_path / "archive"
    _create_archive(selected)
    (selected / ".weishushu_index.json").write_text("{}", encoding="utf-8")

    assert _inspect_without_writes(selected, "10001").state == "archive"


def test_active_wal_is_read_from_external_snapshot_without_changing_source(tmp_path):
    selected = tmp_path / "archive"
    repository = ArchiveRepository.create(
        selected, uid="10001", screen_name="测试用户"
    )
    repository._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    repository.upsert_post(PostRecord(bid="WAL-POST", uid="10001", text="正文"))
    assert (selected / "data" / "archive.db-wal").stat().st_size > 0
    before = _tree_snapshot(selected)

    inspection = inspect_archive_folder(selected, current_uid="10001")

    assert inspection.state == "archive"
    assert inspection.total_posts == 1
    assert _tree_snapshot(selected) == before
    repository.close()


@pytest.mark.parametrize("marker", ["root", "manifest", "data", "database"])
def test_archive_symlink_markers_are_damaged_without_identity_leak(tmp_path, marker):
    require_symlink_capability(target_is_directory=True)
    require_symlink_capability(target_is_directory=False)
    real_archive = tmp_path / "real-archive"
    _create_archive(real_archive, uid="private-uid")
    selected = real_archive
    external = tmp_path / "external"
    external.mkdir()

    if marker == "root":
        selected = tmp_path / "selected-link"
        selected.symlink_to(real_archive, target_is_directory=True)
    elif marker == "manifest":
        source = real_archive / "manifest.json"
        external_manifest = external / "manifest.json"
        source.replace(external_manifest)
        source.symlink_to(external_manifest)
    elif marker == "data":
        source = real_archive / "data"
        external_data = external / "data"
        source.replace(external_data)
        source.symlink_to(external_data, target_is_directory=True)
    else:
        source = real_archive / "data" / "archive.db"
        external_database = external / "archive.db"
        source.replace(external_database)
        source.symlink_to(external_database)

    source_before = _tree_snapshot(selected)
    external_before = _tree_snapshot(external)
    inspection = inspect_archive_folder(selected, current_uid="private-uid")

    assert inspection.state == "damaged"
    assert inspection.uid == ""
    assert inspection.screen_name == ""
    assert inspection.total_posts == 0
    assert "符号链接" in inspection.message
    assert _tree_snapshot(selected) == source_before
    assert _tree_snapshot(external) == external_before


@pytest.mark.parametrize("marker", ["manifest", "data", "database"])
def test_archive_marker_with_wrong_file_type_is_damaged(tmp_path, marker):
    selected = tmp_path / "archive"
    _create_archive(selected)

    if marker == "manifest":
        target = selected / "manifest.json"
        target.unlink()
        target.mkdir()
    elif marker == "data":
        data = selected / "data"
        for child in data.iterdir():
            child.unlink()
        data.rmdir()
        data.write_text("不是目录", encoding="utf-8")
    else:
        target = selected / "data" / "archive.db"
        target.unlink()
        target.mkdir()

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "类型错误" in inspection.message


def test_snapshot_source_that_keeps_changing_returns_chinese_damaged(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    checker_name = (
        "_snapshot_source_unchanged"
        if archive_folder_module._SUPPORTS_DIRECTORY_FDS
        else "_path_snapshot_unchanged"
    )
    monkeypatch.setattr(
        archive_folder_module,
        checker_name,
        lambda *args, **kwargs: False,
    )

    inspection = _inspect_without_writes(selected, "10001")

    assert inspection.state == "damaged"
    assert "持续变化" in inspection.message


def test_snapshot_detects_data_directory_replacement(tmp_path):
    if not archive_folder_module._SUPPORTS_DIRECTORY_FDS:
        pytest.skip("路径快照分支不使用目录文件描述符")
    selected = tmp_path / "archive"
    _create_archive(selected)
    root_fd = os.open(selected, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    data_fd = os.open(
        "data",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    data_identity = archive_folder_module._identity(os.fstat(data_fd))
    (selected / "data").rename(selected / "old-data")
    (selected / "data").mkdir()

    try:
        assert (
            archive_folder_module._snapshot_source_unchanged(
                (), root_fd, data_fd, data_identity, wal_was_absent=True
            )
            is False
        )
    finally:
        os.close(data_fd)
        os.close(root_fd)


def test_snapshot_closes_already_opened_source_when_later_open_fails(
    tmp_path, monkeypatch
):
    if not archive_folder_module._SUPPORTS_DIRECTORY_FDS:
        pytest.skip("路径快照分支不会在复制后保留文件描述符")
    selected = tmp_path / "archive"
    _create_archive(selected)
    real_open = archive_folder_module._open_regular_at
    opened_fds = []
    calls = 0

    def fail_second_open(directory_fd, name):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise archive_folder_module._ArchiveMarkerTypeError(name)
        source = real_open(directory_fd, name)
        opened_fds.append(source.fd)
        return source

    monkeypatch.setattr(archive_folder_module, "_open_regular_at", fail_second_open)

    inspection = inspect_archive_folder(selected, "10001")

    assert inspection.state == "damaged"
    assert opened_fds
    for opened_fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(opened_fd)


def _create_archive_with_posts(root: Path, count: int) -> None:
    repository = ArchiveRepository.create(root, uid="10001", screen_name="新用户")
    for position in range(count):
        repository.upsert_post(
            PostRecord(bid=f"NEW-{position}", uid="10001", text=f"正文{position}")
        )
    repository.close()


def test_manifest_replaced_during_database_validation_retries_new_identity(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    manifest_path = selected / "manifest.json"
    new_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_payload["screen_name"] = "替换后用户"
    real_validate = archive_folder_module._validate_database
    replaced = False

    def replace_manifest(database_path):
        nonlocal replaced
        if not replaced:
            replaced = True
            replacement = selected / "manifest.next"
            replacement.write_text(
                json.dumps(new_payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(replacement, manifest_path)
        return real_validate(database_path)

    monkeypatch.setattr(archive_folder_module, "_validate_database", replace_manifest)

    inspection = inspect_archive_folder(selected, "10001")

    assert inspection.state == "archive"
    assert inspection.screen_name == "替换后用户"


@pytest.mark.parametrize("replacement", ["data", "database"])
def test_database_replaced_during_validation_retries_new_count(
    tmp_path, monkeypatch, replacement
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    prepared = tmp_path / "prepared"
    _create_archive_with_posts(prepared, 2)
    real_validate = archive_folder_module._validate_database
    replaced = False

    def replace_source(database_path):
        nonlocal replaced
        if not replaced:
            replaced = True
            if replacement == "data":
                (selected / "data").rename(tmp_path / "old-data")
                (prepared / "data").rename(selected / "data")
            else:
                os.replace(
                    prepared / "data" / "archive.db",
                    selected / "data" / "archive.db",
                )
        return real_validate(database_path)

    monkeypatch.setattr(archive_folder_module, "_validate_database", replace_source)

    inspection = inspect_archive_folder(selected, "10001")

    assert inspection.state == "archive"
    assert inspection.total_posts == 2


def test_wal_created_with_new_commit_during_validation_retries_new_count(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    assert not (selected / "data" / "archive.db-wal").exists()
    real_validate = archive_folder_module._validate_database
    writers = []

    def create_wal(database_path):
        if not writers:
            writer = ArchiveRepository.open(selected, expected_uid="10001")
            writer.upsert_post(PostRecord(bid="WAL-NEW", uid="10001", text="新正文"))
            writers.append(writer)
        return real_validate(database_path)

    monkeypatch.setattr(archive_folder_module, "_validate_database", create_wal)

    try:
        inspection = inspect_archive_folder(selected, "10001")
        assert inspection.state == "archive"
        assert inspection.total_posts == 2
    finally:
        for writer in writers:
            writer.close()


def test_source_changed_during_every_validation_returns_damaged_without_leak(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected, uid="private-uid")
    manifest_path = selected / "manifest.json"
    real_validate = archive_folder_module._validate_database
    changes = 0

    def keep_replacing_manifest(database_path):
        nonlocal changes
        changes += 1
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["screen_name"] = f"私密用户-{changes}"
        replacement = selected / f"manifest-{changes}.next"
        replacement.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(replacement, manifest_path)
        return real_validate(database_path)

    monkeypatch.setattr(
        archive_folder_module, "_validate_database", keep_replacing_manifest
    )

    inspection = inspect_archive_folder(selected, "private-uid")

    assert inspection.state == "damaged"
    assert inspection.uid == ""
    assert inspection.screen_name == ""
    assert changes == archive_folder_module._SNAPSHOT_ATTEMPTS


def test_manifest_disappears_between_lstat_and_open_then_retries(
    tmp_path, monkeypatch
):
    selected = tmp_path / "archive"
    _create_archive(selected)
    manifest_path = selected / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    real_open = archive_folder_module.os.open
    raced = False

    def race_open(path, flags, *args, **kwargs):
        nonlocal raced
        if path == "manifest.json" and not raced:
            raced = True
            manifest_path.unlink()
            manifest_path.write_bytes(manifest_bytes)
            raise FileNotFoundError("lstat 后瞬间消失")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive_folder_module.os, "open", race_open)

    assert inspect_archive_folder(selected, "10001").state == "archive"


def test_wal_disappears_between_lstat_and_open_then_retries(tmp_path, monkeypatch):
    selected = tmp_path / "archive"
    _create_archive(selected)
    wal_path = selected / "data" / "archive.db-wal"
    wal_path.write_bytes(b"")
    real_open_regular = archive_folder_module._open_regular_at
    raced = False

    def remove_wal(directory_fd, name):
        nonlocal raced
        if name == "archive.db-wal" and not raced:
            raced = True
            wal_path.unlink()
        return real_open_regular(directory_fd, name)

    monkeypatch.setattr(archive_folder_module, "_open_regular_at", remove_wal)

    assert inspect_archive_folder(selected, "10001").state == "archive"


@pytest.mark.parametrize("marker", ["manifest", "database", "wal"])
def test_source_reset_between_lstat_and_open_retries(tmp_path, monkeypatch, marker):
    selected = tmp_path / "archive"
    repository = ArchiveRepository.create(selected, uid="10001", screen_name="测试用户")
    repository._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    repository.upsert_post(PostRecord(bid="WAL-POST", uid="10001", text="正文"))
    repository.close()
    paths = {
        "manifest": selected / "manifest.json",
        "database": selected / "data" / "archive.db",
        "wal": selected / "data" / "archive.db-wal",
    }
    target = paths[marker]
    if marker == "wal" and not target.exists():
        target.write_bytes(b"")
    exact_name = target.name
    original = target.read_bytes()
    real_open = archive_folder_module.os.open
    raced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal raced
        if path == exact_name and not raced:
            raced = True
            replacement = target.with_name(f"{target.name}.next")
            replacement.write_bytes(original)
            os.replace(replacement, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive_folder_module.os, "open", replace_before_open)

    inspection = inspect_archive_folder(selected, "10001")
    assert inspection.state == "archive"
    assert inspection.total_posts == 1


def test_archive_folder_name_sanitizes_nickname():
    assert archive_folder_name("星星点点的碎片", "7148187910") == "星星点点的碎片_7148187910"
    assert archive_folder_name('a/b\\c:d*e?f"g<h>i|j', "1") == "a_b_c_d_e_f_g_h_i_j_1"
    assert archive_folder_name("  多个   空白  ", "1") == "多个 空白_1"
    assert archive_folder_name("...", "1") == "微博书_1"
    assert archive_folder_name("", "1") == "微博书_1"
    assert archive_folder_name("CON", "1") == "CON__1"
    assert archive_folder_name("com1", "1") == "com1__1"
    long_name = "很长的昵称" * 20
    resolved = archive_folder_name(long_name, "1")
    assert resolved.endswith("_1")
    assert len(resolved) <= 62


def test_resolve_archive_dir_returns_archive_as_is(tmp_path):
    selected = tmp_path / "已有微博书"
    _create_archive(selected)

    assert resolve_archive_dir(selected, "10001", "本人") == str(selected)


def test_resolve_archive_dir_finds_renamed_child_archive(tmp_path):
    child = tmp_path / "旧昵称_10001"
    _create_archive(child)
    (tmp_path / "无关文件.txt").write_text("别动我", encoding="utf-8")
    (tmp_path / "普通子目录").mkdir()

    resolved = resolve_archive_dir(tmp_path, "10001", "新昵称")

    assert resolved == str(child)


def test_resolve_archive_dir_rejects_multiple_child_archives(tmp_path):
    _create_archive(tmp_path / "微博书A")
    _create_archive(tmp_path / "微博书B")

    with pytest.raises(WeiboError, match="多个当前账号的微博书"):
        resolve_archive_dir(tmp_path, "10001", "本人")


def test_resolve_archive_dir_ignores_other_accounts_child(tmp_path):
    _create_archive(tmp_path / "别人_20002", uid="20002")

    resolved = resolve_archive_dir(tmp_path, "10001", "本人")

    assert resolved == str(tmp_path / "本人_10001")


def test_resolve_archive_dir_nests_new_archive_for_plain_folder(tmp_path):
    for selected in (tmp_path / "空目录", tmp_path / "不存在"):
        if selected.name == "空目录":
            selected.mkdir()
        assert resolve_archive_dir(selected, "10001", "本人") == str(
            selected / "本人_10001"
        )


def test_resolve_archive_dir_is_idempotent(tmp_path):
    target = tmp_path / "本人_10001"

    assert resolve_archive_dir(target, "10001", "本人") == str(target)
    _create_archive(target)
    assert resolve_archive_dir(target, "10001", "本人") == str(target)


def test_resolve_archive_dir_rejects_occupied_target_name(tmp_path):
    target = tmp_path / "本人_10001"
    target.mkdir()
    (target / "其他文件.txt").write_text("占用", encoding="utf-8")

    with pytest.raises(WeiboError, match="已存在且不是微博书"):
        resolve_archive_dir(tmp_path, "10001", "本人")


def test_resolve_archive_dir_is_read_only(tmp_path):
    child = tmp_path / "旧昵称_10001"
    _create_archive(child)
    (tmp_path / "无关文件.txt").write_text("别动我", encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    resolve_archive_dir(tmp_path, "10001", "新昵称")

    assert _tree_snapshot(tmp_path) == before


def test_inspect_selected_folder_follows_single_child_archive(tmp_path):
    child = tmp_path / "旧昵称_10001"
    _create_archive(child)
    (tmp_path / "无关文件.txt").write_text("别动我", encoding="utf-8")

    inspection = inspect_selected_folder(tmp_path, "10001")

    assert inspection.state == "archive"
    assert inspection.path == str(child)
    assert inspection.uid == "10001"


def test_inspect_selected_folder_keeps_original_when_not_unique(tmp_path):
    _create_archive(tmp_path / "微博书A")
    _create_archive(tmp_path / "微博书B")

    inspection = inspect_selected_folder(tmp_path, "10001")

    assert inspection.state == "ordinary_nonempty"
    assert inspection.path == str(tmp_path)


def test_inspect_selected_folder_passes_through_non_ordinary_states(tmp_path):
    archive = tmp_path / "已是微博书"
    _create_archive(archive)
    empty = tmp_path / "空目录"
    empty.mkdir()

    assert inspect_selected_folder(archive, "10001").path == str(archive)
    assert inspect_selected_folder(empty, "10001").state == "empty"
