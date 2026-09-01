import json
import sqlite3
import threading
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import get_type_hints

import pytest


def _create_repository(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    return ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )


def _post(**changes):
    from weibo_book.archive.schema import PostRecord

    values = {
        "bid": "P001",
        "uid": "10001",
        "text": "微博正文",
    }
    values.update(changes)
    return PostRecord(**values)


def test_create_writes_manifest_and_exact_v2_schema(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")

    assert json.loads((root / "manifest.json").read_text(encoding="utf-8")) == {
        "created_at": repository.manifest().created_at,
        "last_successful_sync_at": "",
        "schema_version": 2,
        "screen_name": "测试用户",
        "uid": "10001",
    }
    connection = sqlite3.connect(root / "data" / "archive.db")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if not row[0].startswith("sqlite_")
    }
    assert tables == {
        "archive_meta",
        "posts",
        "post_revisions",
        "comments",
        "media",
        "sync_runs",
        "following_snapshots",
        "following_snapshot_items",
        "following_objects",
        "following_relationships",
        "following_names",
        "following_changes",
        "following_state",
    }
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert repository._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert json.loads(
        connection.execute(
            "SELECT value_json FROM archive_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    ) == 2

    expected_columns = {
        "archive_meta": [
            (0, "key", "TEXT", 0, None, 1),
            (1, "value_json", "TEXT", 1, None, 0),
        ],
        "posts": [
            (0, "bid", "TEXT", 0, None, 1),
            (1, "uid", "TEXT", 1, None, 0),
            (2, "text", "TEXT", 1, None, 0),
            (3, "created_at", "TEXT", 1, None, 0),
            (4, "source", "TEXT", 1, None, 0),
            (5, "ip_location", "TEXT", 1, None, 0),
            (6, "is_pinned", "INTEGER", 1, None, 0),
            (7, "pin_order", "INTEGER", 0, None, 0),
            (8, "visibility", "TEXT", 1, None, 0),
            (9, "reposts_count", "INTEGER", 1, None, 0),
            (10, "comments_count", "INTEGER", 1, None, 0),
            (11, "likes_count", "INTEGER", 1, None, 0),
            (12, "retweeted_json", "TEXT", 0, None, 0),
            (13, "link_card_json", "TEXT", 0, None, 0),
            (14, "media_signature_json", "TEXT", 1, None, 0),
        ],
        "post_revisions": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "bid", "TEXT", 1, None, 0),
            (2, "revision_no", "INTEGER", 1, None, 0),
            (3, "captured_at", "TEXT", 1, None, 0),
            (4, "payload_json", "TEXT", 1, None, 0),
            (5, "content_hash", "TEXT", 1, None, 0),
        ],
        "comments": [
            (0, "id", "TEXT", 0, None, 1),
            (1, "post_bid", "TEXT", 1, None, 0),
            (2, "parent_id", "TEXT", 0, None, 0),
            (3, "payload_json", "TEXT", 1, None, 0),
            (4, "captured_at", "TEXT", 1, None, 0),
        ],
        "media": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "owner_type", "TEXT", 1, None, 0),
            (2, "owner_id", "TEXT", 1, None, 0),
            (3, "role", "TEXT", 1, None, 0),
            (4, "position", "INTEGER", 1, None, 0),
            (5, "remote_url", "TEXT", 1, None, 0),
            (6, "local_path", "TEXT", 1, None, 0),
            (7, "sha256", "TEXT", 1, None, 0),
        ],
        "sync_runs": [
            (0, "run_id", "TEXT", 0, None, 1),
            (1, "mode", "TEXT", 1, None, 0),
            (2, "status", "TEXT", 1, None, 0),
            (3, "started_at", "TEXT", 1, None, 0),
            (4, "finished_at", "TEXT", 1, "''", 0),
            (5, "summary_json", "TEXT", 1, "'{}'", 0),
            (6, "checkpoint_json", "TEXT", 1, "'{}'", 0),
        ],
        "following_snapshots": [
            (0, "snapshot_id", "TEXT", 0, None, 1),
            (1, "status", "TEXT", 1, None, 0),
            (2, "started_at", "TEXT", 1, None, 0),
            (3, "cutoff_at", "TEXT", 1, "''", 0),
            (4, "bloggers_complete", "INTEGER", 1, "0", 0),
            (5, "supertopics_complete", "INTEGER", 1, "0", 0),
            (6, "blogger_reported_total", "INTEGER", 0, None, 0),
            (7, "supertopic_reported_total", "INTEGER", 0, None, 0),
            (8, "completed_at", "TEXT", 1, "''", 0),
            (9, "summary_json", "TEXT", 1, "'{}'", 0),
        ],
        "following_snapshot_items": [
            (0, "snapshot_id", "TEXT", 1, None, 1),
            (1, "object_type", "TEXT", 1, None, 2),
            (2, "object_id", "TEXT", 1, None, 3),
            (3, "display_name", "TEXT", 1, None, 0),
            (4, "page_url", "TEXT", 1, None, 0),
            (5, "app_scheme", "TEXT", 1, None, 0),
            (6, "source_order", "INTEGER", 1, None, 0),
            (7, "platform_followed_at", "TEXT", 1, "''", 0),
        ],
        "following_objects": [
            (0, "object_type", "TEXT", 1, None, 1),
            (1, "object_id", "TEXT", 1, None, 2),
            (2, "current_name", "TEXT", 1, None, 0),
            (3, "page_url", "TEXT", 1, None, 0),
            (4, "app_scheme", "TEXT", 1, None, 0),
            (5, "first_seen_at", "TEXT", 1, None, 0),
            (6, "last_seen_at", "TEXT", 1, None, 0),
        ],
        "following_relationships": [
            (0, "relationship_id", "INTEGER", 0, None, 1),
            (1, "object_type", "TEXT", 1, None, 0),
            (2, "object_id", "TEXT", 1, None, 0),
            (3, "started_snapshot_id", "TEXT", 1, None, 0),
            (4, "ended_snapshot_id", "TEXT", 0, None, 0),
            (5, "local_first_seen_at", "TEXT", 1, None, 0),
            (6, "last_confirmed_at", "TEXT", 1, None, 0),
            (7, "platform_followed_at", "TEXT", 1, "''", 0),
        ],
        "following_names": [
            (0, "name_record_id", "INTEGER", 0, None, 1),
            (1, "object_type", "TEXT", 1, None, 0),
            (2, "object_id", "TEXT", 1, None, 0),
            (3, "name", "TEXT", 1, None, 0),
            (4, "started_snapshot_id", "TEXT", 1, None, 0),
            (5, "ended_snapshot_id", "TEXT", 0, None, 0),
            (6, "first_seen_at", "TEXT", 1, None, 0),
            (7, "last_seen_at", "TEXT", 1, None, 0),
        ],
        "following_changes": [
            (0, "change_id", "INTEGER", 0, None, 1),
            (1, "snapshot_id", "TEXT", 1, None, 0),
            (2, "change_type", "TEXT", 1, None, 0),
            (3, "object_type", "TEXT", 1, None, 0),
            (4, "object_id", "TEXT", 1, None, 0),
            (5, "before_json", "TEXT", 1, "'{}'", 0),
            (6, "after_json", "TEXT", 1, "'{}'", 0),
        ],
        "following_state": [
            (0, "singleton", "INTEGER", 0, None, 1),
            (1, "current_snapshot_id", "TEXT", 0, None, 0),
        ],
    }
    for table, columns in expected_columns.items():
        assert connection.execute(f"PRAGMA table_info({table})").fetchall() == columns

    assert connection.execute("PRAGMA foreign_key_list(post_revisions)").fetchall() == [
        (0, 0, "posts", "bid", "bid", "NO ACTION", "CASCADE", "NONE")
    ]
    assert connection.execute("PRAGMA foreign_key_list(comments)").fetchall() == [
        (0, 0, "posts", "post_bid", "bid", "NO ACTION", "CASCADE", "NONE")
    ]

    def unique_index_columns(table):
        result = []
        for _, name, unique, _, _ in connection.execute(
            f"PRAGMA index_list({table})"
        ):
            if unique:
                result.append(
                    tuple(
                        row[2]
                        for row in connection.execute(f"PRAGMA index_info({name})")
                    )
                )
        return set(result)

    assert unique_index_columns("post_revisions") == {("bid", "revision_no")}
    assert unique_index_columns("media") == {
        ("owner_type", "owner_id", "role", "position")
    }

    connection.close()
    repository.close()


def test_create_writes_schema_two_following_tables(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    tables = {
        row[0]
        for row in repository._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if not row[0].startswith("sqlite_")
    }
    assert {
        "following_snapshots",
        "following_snapshot_items",
        "following_objects",
        "following_relationships",
        "following_names",
        "following_changes",
        "following_state",
    } <= tables
    assert repository._connection.execute(
        "SELECT singleton, current_snapshot_id FROM following_state"
    ).fetchall() == [(1, None)]

    repository.close()


def test_create_rejects_nonempty_target(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = tmp_path / "archive"
    root.mkdir()
    (root / "existing.txt").write_text("已有内容", encoding="utf-8")

    with pytest.raises(ArchiveError, match="不是空目录"):
        ArchiveRepository.create(root, uid="10001", screen_name="测试用户")


def test_create_converts_sqlite_failure_to_archive_error(tmp_path, monkeypatch):
    from weibo_book.archive import repository as repository_module
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("模拟 SQLite 连接失败")

    monkeypatch.setattr(repository_module.sqlite3, "connect", fail_connect)
    with pytest.raises(ArchiveError, match="创建归档失败"):
        ArchiveRepository.create(
            tmp_path / "archive", uid="10001", screen_name="测试用户"
        )


def test_transaction_rolls_back_and_preserves_non_database_exception(tmp_path):
    repository = _create_repository(tmp_path)

    with pytest.raises(RuntimeError, match="测试回滚"):
        with repository.transaction():
            repository.upsert_post(_post())
            raise RuntimeError("测试回滚")

    assert repository.get_post("P001") is None
    repository.close()


def test_transaction_declares_context_manager_return_type():
    from weibo_book.archive.repository import ArchiveRepository

    assert (
        get_type_hints(ArchiveRepository.transaction)["return"]
        == AbstractContextManager[None]
    )


def test_open_rejects_archive_owned_by_another_uid(tmp_path):
    from weibo_book.archive.repository import ArchiveIdentityError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()

    with pytest.raises(ArchiveIdentityError, match="属于其他账号"):
        ArchiveRepository.open(root, expected_uid="20002")


def test_post_roundtrip_restores_json_nullable_fields_and_upsert_updates(tmp_path):
    repository = _create_repository(tmp_path)
    complete = _post(
        created_at="2026-07-14T01:02:03+00:00",
        source="iPhone 客户端",
        ip_location="发布于 测试地区",
        is_pinned=True,
        pin_order=2,
        visibility="visible",
        reposts_count=3,
        comments_count=4,
        likes_count=5,
        retweeted_payload={"bid": "RP001", "text": "转发原文"},
        link_card_payload={"title": "链接卡片", "url": "https://example.test"},
        media_signature=[{"role": "image", "position": 0}],
    )

    repository.upsert_post(complete)
    assert repository.get_post("P001") == complete
    assert repository.list_known_bids() == {"P001"}

    updated = _post(
        text="更新后正文",
        retweeted_payload=None,
        link_card_payload=None,
        media_signature=[],
    )
    repository.upsert_post(updated)
    assert repository.get_post("P001") == updated
    repository.close()


def test_revision_uniqueness_and_comment_replacement(tmp_path):
    from weibo_book.archive.repository import ArchiveError
    from weibo_book.archive.schema import CommentRecord, PostRevisionRecord

    repository = _create_repository(tmp_path)
    repository.upsert_post(_post())
    revision = PostRevisionRecord(
        bid="P001",
        revision_no=1,
        captured_at="2026-07-14T01:00:00+00:00",
        payload={"text": "初始正文"},
        content_hash="hash-1",
    )
    repository.add_post_revision(revision)
    with pytest.raises(ArchiveError):
        repository.add_post_revision(revision)

    repository.replace_current_comments(
        "P001",
        [
            CommentRecord(
                id="C001",
                post_bid="P001",
                parent_id=None,
                payload={"text": "旧评论"},
                captured_at="2026-07-14T01:01:00+00:00",
            ),
            CommentRecord(
                id="C002",
                post_bid="P001",
                parent_id="C001",
                payload={"text": "旧回复"},
                captured_at="2026-07-14T01:02:00+00:00",
            ),
        ],
    )
    repository.replace_current_comments(
        "P001",
        [
            CommentRecord(
                id="C003",
                post_bid="P001",
                parent_id=None,
                payload={"text": "新评论"},
                captured_at="2026-07-14T01:03:00+00:00",
            )
        ],
    )
    rows = repository._connection.execute(
        "SELECT id, payload_json FROM comments ORDER BY id"
    ).fetchall()
    assert rows == [("C003", json.dumps({"text": "新评论"}, ensure_ascii=False, sort_keys=True))]
    repository.close()


def test_render_read_methods_return_schema_records_in_stable_row_order(tmp_path):
    from weibo_book.archive.schema import CommentRecord, MediaRecord

    repository = _create_repository(tmp_path)
    repository.upsert_post(_post(bid="B"))
    repository.upsert_post(_post(bid="A"))
    repository.replace_current_comments(
        "A", [CommentRecord("C", "A", None, {"text": "评论"}, "captured")]
    )
    repository.upsert_media(MediaRecord("post", "A", "image", 0, "remote", "media/a.jpg"))

    assert [row.bid for row in repository.list_posts_for_render()] == ["B", "A"]
    assert repository.list_comments_for_render()[0].payload == {"text": "评论"}
    assert repository.list_media_for_render()[0].role == "image"
    repository.close()


def test_comment_replacement_commits_inside_outer_transaction(tmp_path):
    from weibo_book.archive.schema import CommentRecord

    repository = _create_repository(tmp_path)
    repository.upsert_post(_post())
    comment = CommentRecord(
        id="C001",
        post_bid="P001",
        parent_id=None,
        payload={"text": "外层事务评论"},
        captured_at="2026-07-14T01:00:00+00:00",
    )

    with repository.transaction():
        repository.replace_current_comments("P001", [comment])

    assert repository._connection.execute(
        "SELECT id FROM comments"
    ).fetchall() == [("C001",)]
    repository.close()


def test_comment_replacement_rolls_back_with_outer_transaction(tmp_path):
    from weibo_book.archive.schema import CommentRecord

    repository = _create_repository(tmp_path)
    repository.upsert_post(_post())
    old_comment = CommentRecord(
        id="C001",
        post_bid="P001",
        parent_id=None,
        payload={"text": "旧评论"},
        captured_at="2026-07-14T01:00:00+00:00",
    )
    new_comment = CommentRecord(
        id="C002",
        post_bid="P001",
        parent_id=None,
        payload={"text": "新评论"},
        captured_at="2026-07-14T02:00:00+00:00",
    )
    repository.replace_current_comments("P001", [old_comment])

    with pytest.raises(RuntimeError, match="回滚外层事务"):
        with repository.transaction():
            repository.replace_current_comments("P001", [new_comment])
            raise RuntimeError("回滚外层事务")

    assert repository._connection.execute(
        "SELECT id FROM comments"
    ).fetchall() == [("C001",)]
    repository.close()


@pytest.mark.parametrize(
    "local_path",
    [
        "",
        ".",
        "..",
        "/absolute/file.jpg",
        "media\\file.jpg",
        "media/../file.jpg",
        "media//x.jpg",
        "media/",
        "data/archive.db",
        "manifest.json",
        ".work/run/file.jpg",
        "assets/file.jpg",
    ],
)
def test_media_rejects_non_posix_relative_local_path(tmp_path, local_path):
    from weibo_book.archive.repository import ArchiveError
    from weibo_book.archive.schema import MediaRecord

    repository = _create_repository(tmp_path)
    with pytest.raises(ArchiveError, match="POSIX 相对路径"):
        repository.upsert_media(
            MediaRecord("post", "P001", "image", 0, "https://example.test/1", local_path)
        )
    repository.close()


def test_media_unique_key_is_upserted(tmp_path):
    from weibo_book.archive.schema import MediaRecord

    repository = _create_repository(tmp_path)
    repository.upsert_media(
        MediaRecord("post", "P001", "image", 0, "https://example.test/old", "media/1.jpg")
    )
    repository.upsert_media(
        MediaRecord(
            "post",
            "P001",
            "image",
            0,
            "https://example.test/new",
            "media/1-new.jpg",
            "sha-new",
        )
    )
    assert repository._connection.execute(
        "SELECT remote_url, local_path, sha256 FROM media"
    ).fetchall() == [("https://example.test/new", "media/1-new.jpg", "sha-new")]
    repository.close()


def test_begin_and_finish_sync_write_status_timestamps_and_sorted_json(tmp_path):
    repository = _create_repository(tmp_path)

    run_id = repository.begin_sync("incremental")
    running = repository._connection.execute(
        "SELECT mode, status, started_at, finished_at, summary_json, checkpoint_json "
        "FROM sync_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert running[0:2] == ("incremental", "running")
    assert running[2]
    assert running[3:] == ("", "{}", "{}")

    repository.finish_sync(run_id, "done", {"z": 1, "message": "完成", "a": 2})
    finished = repository._connection.execute(
        "SELECT status, finished_at, summary_json FROM sync_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert finished[0] == "done"
    assert finished[1]
    assert finished[2] == '{"a": 2, "message": "完成", "z": 1}'
    repository.close()


def test_finish_sync_rejects_unknown_run_id(tmp_path):
    from weibo_book.archive.repository import ArchiveError

    repository = _create_repository(tmp_path)

    with pytest.raises(ArchiveError, match="同步记录不存在"):
        repository.finish_sync("unknown-run-id", "done", {})

    repository.close()


def test_manifest_write_failure_leaves_no_manifest_or_temporary_file(tmp_path, monkeypatch):
    from weibo_book.archive import repository as repository_module
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = tmp_path / "archive"

    def fail_replace(source, destination):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(repository_module.os, "replace", fail_replace)
    with pytest.raises(ArchiveError, match="创建归档失败"):
        ArchiveRepository.create(root, uid="10001", screen_name="测试用户")

    assert not (root / "manifest.json").exists()
    assert list(root.glob("*manifest*.tmp")) == []


def test_manifest_write_failure_can_immediately_retry_create(tmp_path, monkeypatch):
    from weibo_book.archive import repository as repository_module
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = tmp_path / "archive"
    root.mkdir()
    real_replace = repository_module.os.replace
    replace_calls = 0

    def fail_once(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("模拟首次替换失败")
        real_replace(source, destination)

    monkeypatch.setattr(repository_module.os, "replace", fail_once)
    with pytest.raises(ArchiveError, match="创建归档失败"):
        ArchiveRepository.create(root, uid="10001", screen_name="测试用户")

    assert root.is_dir()
    assert list(root.iterdir()) == []

    repository = ArchiveRepository.create(
        root, uid="10001", screen_name="测试用户"
    )
    assert repository.manifest().uid == "10001"
    repository.close()


def test_open_reports_damaged_manifest_in_chinese(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = tmp_path / "archive"
    root.mkdir()
    (root / "manifest.json").write_text("{damaged", encoding="utf-8")

    with pytest.raises(ArchiveError, match="归档清单损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_reports_non_utf8_manifest_in_chinese(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    root = tmp_path / "archive"
    root.mkdir()
    (root / "manifest.json").write_bytes(b"\xff\xfe")

    with pytest.raises(ArchiveError, match="归档清单损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_reports_missing_database_in_chinese(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()
    (root / "data" / "archive.db").unlink()

    with pytest.raises(ArchiveError, match="归档数据库不存在"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_reports_damaged_database_in_chinese(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()
    (root / "data" / "archive.db").write_bytes(b"not a sqlite database")

    with pytest.raises(ArchiveError, match="归档数据库损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_rejects_database_missing_required_table(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository._connection.execute("DROP TABLE media")
    repository.close()

    with pytest.raises(ArchiveError, match="归档数据库损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_rejects_database_missing_required_column(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository._connection.execute("ALTER TABLE posts DROP COLUMN source")
    repository.close()

    with pytest.raises(ArchiveError, match="归档数据库损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_rejects_database_missing_required_unique_constraint(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository._connection.execute("ALTER TABLE post_revisions RENAME TO old_revisions")
    repository._connection.execute(
        """
        CREATE TABLE post_revisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bid TEXT NOT NULL REFERENCES posts(bid) ON DELETE CASCADE,
            revision_no INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """
    )
    repository._connection.execute("DROP TABLE old_revisions")
    repository.close()

    with pytest.raises(ArchiveError, match="归档数据库损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_rejects_unsupported_manifest_schema_version(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()
    manifest_path = root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 3
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArchiveError, match="归档版本不受支持：3"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_open_migrates_exact_schema_one_without_changing_existing_data(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.upsert_post(_post())
    repository._connection.commit()
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
    repository._connection.commit()
    repository.close()
    manifest_path = root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = ArchiveRepository.open(root, expected_uid="10001")

    assert migrated.manifest().schema_version == 2
    assert migrated.get_post("P001") == _post()
    migrated.upsert_post(_post(bid="P002", text="迁移后新增"))
    assert [item.bid for item in migrated.list_posts_for_render()] == ["P001", "P002"]
    assert migrated._connection.execute(
        "SELECT singleton, current_snapshot_id FROM following_state"
    ).fetchall() == [(1, None)]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema_version"] == 2
    migrated.close()


def test_open_coordinates_schema_two_database_with_schema_one_manifest(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()
    manifest_path = root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    coordinated = ArchiveRepository.open(root, expected_uid="10001")

    assert coordinated.manifest().schema_version == 2
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema_version"] == 2
    coordinated.close()


def test_schema_one_migration_rolls_back_added_tables_when_version_update_fails():
    from weibo_book.archive import schema as schema_module

    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.executescript(schema_module._SCHEMA_V1)
    connection.execute(
        "INSERT INTO archive_meta(key,value_json) VALUES ('schema_version','1')"
    )
    connection.execute(
        """
        CREATE TRIGGER fail_schema_version_update
        BEFORE UPDATE ON archive_meta
        WHEN NEW.key='schema_version'
        BEGIN
            SELECT RAISE(ABORT,'模拟版本更新失败');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="模拟版本更新失败"):
        schema_module.migrate_schema(connection, 1)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if not row[0].startswith("sqlite_")
    }
    assert tables == set(schema_module._EXPECTED_V1_TABLE_COLUMNS)
    assert schema_module.read_schema_version(connection) == 1
    connection.close()


def test_open_rejects_following_relationship_index_without_partial_predicate(tmp_path):
    from weibo_book.archive.repository import ArchiveError, ArchiveRepository

    repository = _create_repository(tmp_path)
    root = repository._root
    repository.close()
    connection = sqlite3.connect(root / "data" / "archive.db")
    connection.execute("DROP INDEX following_relationships_one_open")
    connection.execute(
        "CREATE UNIQUE INDEX following_relationships_one_open "
        "ON following_relationships(object_type,object_id)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ArchiveError, match="归档数据库损坏"):
        ArchiveRepository.open(root, expected_uid="10001")


def test_content_fingerprint_uses_exact_content_fields_only():
    from weibo_book.archive.fingerprint import CONTENT_FIELDS, content_fingerprint

    assert CONTENT_FIELDS == (
        "text",
        "source",
        "ip_location",
        "is_pinned",
        "visibility",
        "retweeted_payload",
        "link_card_payload",
        "media_signature",
    )
    original = _post(
        source="iPhone 客户端",
        ip_location="发布于 测试地区",
        is_pinned=True,
        visibility="visible",
        reposts_count=1,
        comments_count=2,
        likes_count=3,
        retweeted_payload={"bid": "RP001", "text": "转发原文"},
        link_card_payload={"title": "链接卡片"},
        media_signature=[{"role": "image", "position": 0}],
    )

    for field_name in ("reposts_count", "comments_count", "likes_count"):
        changed = replace(original, **{field_name: getattr(original, field_name) + 1})
        assert content_fingerprint(changed) == content_fingerprint(original)

    content_changes = {
        "text": "更新后正文",
        "source": "Web 客户端",
        "ip_location": "发布于 其他地区",
        "is_pinned": False,
        "visibility": "hidden",
        "retweeted_payload": {"bid": "RP002", "text": "新转发原文"},
        "link_card_payload": {"title": "新链接卡片"},
        "media_signature": [{"role": "video", "position": 0}],
    }
    for field_name, value in content_changes.items():
        assert content_fingerprint(replace(original, **{field_name: value})) != content_fingerprint(
            original
        )


def test_apply_post_change_classifies_new_unchanged_and_counts_change(tmp_path):
    repository = _create_repository(tmp_path)
    original = _post(likes_count=1)

    assert repository.apply_post_change(original).kind == "new"
    assert repository.get_post("P001") == original
    assert repository.list_revisions("P001") == []

    assert repository.apply_post_change(original).kind == "unchanged"
    assert repository.list_revisions("P001") == []

    counts_updated = replace(
        original, reposts_count=2, comments_count=3, likes_count=4
    )
    assert repository.apply_post_change(counts_updated).kind == "counts_changed"
    assert repository.get_post("P001") == counts_updated
    assert repository.list_revisions("P001") == []
    repository.close()


def test_retweeted_avatar_and_counts_do_not_create_content_revision(tmp_path):
    repository = _create_repository(tmp_path)
    original = _post(retweeted_payload={
        "bid": "RP001",
        "text": "转发原文",
        "user_avatar": "https://example.invalid/avatar?token=old",
        "reposts_count": 1,
        "comments_count": 2,
        "likes_count": 3,
    })
    updated = replace(original, retweeted_payload={
        "bid": "RP001",
        "text": "转发原文",
        "user_avatar": "https://example.invalid/avatar?token=new",
        "reposts_count": 4,
        "comments_count": 5,
        "likes_count": 6,
    })

    repository.apply_post_change(original)
    assert repository.apply_post_change(updated).kind == "counts_changed"
    assert repository.get_post("P001") == updated
    assert repository.list_revisions("P001") == []
    repository.close()


def test_apply_post_change_saves_consecutive_complete_old_versions(tmp_path):
    from weibo_book.archive.fingerprint import content_fingerprint

    repository = _create_repository(tmp_path)
    original = _post(
        text="第一版",
        source="iPhone 客户端",
        ip_location="发布于 测试地区",
        is_pinned=True,
        pin_order=1,
        reposts_count=1,
        comments_count=2,
        likes_count=3,
        retweeted_payload={"bid": "RP001"},
        link_card_payload={"title": "链接"},
        media_signature=[{"role": "image", "position": 0}],
    )
    second = replace(original, text="第二版", likes_count=4)
    third = replace(second, text="第三版", comments_count=5)

    repository.apply_post_change(original)
    assert repository.apply_post_change(second).kind == "content_changed"
    assert repository.apply_post_change(third).kind == "content_changed"

    revisions = repository.list_revisions("P001")
    assert [revision.revision_no for revision in revisions] == [1, 2]
    assert [revision.payload["text"] for revision in revisions] == ["第一版", "第二版"]
    assert revisions[0].payload == {
        "bid": "P001",
        "uid": "10001",
        "text": "第一版",
        "created_at": "",
        "source": "iPhone 客户端",
        "ip_location": "发布于 测试地区",
        "is_pinned": True,
        "pin_order": 1,
        "visibility": "visible",
        "reposts_count": 1,
        "comments_count": 2,
        "likes_count": 3,
        "retweeted_payload": {"bid": "RP001"},
        "link_card_payload": {"title": "链接"},
        "media_signature": [{"role": "image", "position": 0}],
    }
    assert revisions[0].captured_at
    assert revisions[1].captured_at
    assert revisions[0].content_hash == content_fingerprint(original)
    assert revisions[1].content_hash == content_fingerprint(second)
    assert repository.get_post("P001") == third
    repository.close()


def test_apply_post_change_rolls_back_revision_and_current_with_outer_failure(tmp_path):
    repository = _create_repository(tmp_path)
    original = _post(text="旧正文")
    repository.apply_post_change(original)

    with pytest.raises(RuntimeError, match="模拟后续失败"):
        with repository.transaction():
            result = repository.apply_post_change(replace(original, text="新正文"))
            assert result.kind == "content_changed"
            raise RuntimeError("模拟后续失败")

    assert repository.get_post("P001") == original
    assert repository.list_revisions("P001") == []
    repository.close()


def test_concurrent_content_writers_serialize_before_reading_current(
    tmp_path, monkeypatch
):
    from weibo_book.archive.repository import ArchiveRepository

    creator = _create_repository(tmp_path)
    root = creator._root
    original = _post(text="旧版")
    creator.apply_post_change(original)
    creator.close()

    first_read = threading.Event()
    second_begin_seen = threading.Event()
    release_first = threading.Event()
    first_done = threading.Event()
    real_get_post = ArchiveRepository.get_post
    results = {}
    errors = []

    def coordinated_get_post(repository, bid):
        post = real_get_post(repository, bid)
        thread_name = threading.current_thread().name
        if thread_name == "archive-writer-one":
            first_read.set()
            if not release_first.wait(timeout=5):
                raise RuntimeError("第一个写者等待放行超时")
        elif thread_name == "archive-writer-two":
            if not first_done.wait(timeout=5):
                raise RuntimeError("第二个写者等待首次提交超时")
        return post

    monkeypatch.setattr(ArchiveRepository, "get_post", coordinated_get_post)

    def write(thread_label, post, *, observe_begin=False):
        repository = None
        try:
            repository = ArchiveRepository.open(root, expected_uid="10001")
            if observe_begin:
                repository._connection.set_trace_callback(
                    lambda statement: second_begin_seen.set()
                    if statement.startswith("BEGIN")
                    else None
                )
            results[thread_label] = repository.apply_post_change(post).kind
        except BaseException as exc:
            errors.append((thread_label, exc))
        finally:
            if repository is not None:
                repository.close()
            if thread_label == "first":
                first_done.set()

    first = threading.Thread(
        target=write,
        args=("first", replace(original, text="中间版")),
        name="archive-writer-one",
    )
    second = threading.Thread(
        target=write,
        args=("second", replace(original, text="最终版")),
        kwargs={"observe_begin": True},
        name="archive-writer-two",
    )

    first.start()
    assert first_read.wait(timeout=5), "第一个写者未进入读取阶段"
    second.start()
    assert second_begin_seen.wait(timeout=5), "第二个写者未开始事务"
    release_first.set()

    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive(), "第一个写者发生死锁"
    assert not second.is_alive(), "第二个写者发生死锁"
    assert errors == []
    assert results == {"first": "content_changed", "second": "content_changed"}

    verifier = ArchiveRepository.open(root, expected_uid="10001")
    revisions = verifier.list_revisions("P001")
    assert [revision.revision_no for revision in revisions] == [1, 2]
    assert [revision.payload["text"] for revision in revisions] == ["旧版", "中间版"]
    assert verifier.get_post("P001").text == "最终版"
    verifier.close()
