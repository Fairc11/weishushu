"""SQLite 归档结构、精确校验和单向迁移。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .media_layout import MEDIA_LAYOUT_VERSION, MEDIA_LAYOUT_VERSION_KEY


SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "archive.db"


@dataclass(frozen=True)
class ArchiveManifest:
    schema_version: int
    uid: str
    screen_name: str
    created_at: str
    last_successful_sync_at: str = ""


@dataclass(frozen=True)
class PostRecord:
    bid: str
    uid: str
    text: str
    created_at: str = ""
    source: str = ""
    ip_location: str = ""
    is_pinned: bool = False
    pin_order: int | None = None
    visibility: str = "visible"
    reposts_count: int = 0
    comments_count: int = 0
    likes_count: int = 0
    retweeted_payload: dict | None = None
    link_card_payload: dict | None = None
    media_signature: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PostRevisionRecord:
    bid: str
    revision_no: int
    captured_at: str
    payload: dict
    content_hash: str


@dataclass(frozen=True)
class CommentRecord:
    id: str
    post_bid: str
    parent_id: str | None
    payload: dict
    captured_at: str


@dataclass(frozen=True)
class MediaRecord:
    owner_type: str
    owner_id: str
    role: str
    position: int
    remote_url: str
    local_path: str
    sha256: str = ""


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS archive_meta(
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts(
    bid TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    ip_location TEXT NOT NULL,
    is_pinned INTEGER NOT NULL,
    pin_order INTEGER,
    visibility TEXT NOT NULL,
    reposts_count INTEGER NOT NULL,
    comments_count INTEGER NOT NULL,
    likes_count INTEGER NOT NULL,
    retweeted_json TEXT,
    link_card_json TEXT,
    media_signature_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_revisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bid TEXT NOT NULL REFERENCES posts(bid) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(bid,revision_no)
);
CREATE TABLE IF NOT EXISTS comments(
    id TEXT PRIMARY KEY,
    post_bid TEXT NOT NULL REFERENCES posts(bid) ON DELETE CASCADE,
    parent_id TEXT,
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    role TEXT NOT NULL,
    position INTEGER NOT NULL,
    remote_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    UNIQUE(owner_type,owner_id,role,position)
);
CREATE TABLE IF NOT EXISTS sync_runs(
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}'
);
"""

_SCHEMA_V2_ADDITIONS = """
CREATE TABLE IF NOT EXISTS following_snapshots(
    snapshot_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('staging','complete')),
    started_at TEXT NOT NULL,
    cutoff_at TEXT NOT NULL DEFAULT '',
    bloggers_complete INTEGER NOT NULL DEFAULT 0,
    supertopics_complete INTEGER NOT NULL DEFAULT 0,
    blogger_reported_total INTEGER,
    supertopic_reported_total INTEGER,
    completed_at TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS following_snapshot_items(
    snapshot_id TEXT NOT NULL REFERENCES following_snapshots(snapshot_id) ON DELETE CASCADE,
    object_type TEXT NOT NULL CHECK(object_type IN ('blogger','supertopic')),
    object_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    page_url TEXT NOT NULL,
    app_scheme TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK(source_order >= 0),
    platform_followed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(snapshot_id,object_type,object_id),
    UNIQUE(snapshot_id,object_type,source_order)
);
CREATE TABLE IF NOT EXISTS following_objects(
    object_type TEXT NOT NULL CHECK(object_type IN ('blogger','supertopic')),
    object_id TEXT NOT NULL,
    current_name TEXT NOT NULL,
    page_url TEXT NOT NULL,
    app_scheme TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(object_type,object_id)
);
CREATE TABLE IF NOT EXISTS following_relationships(
    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    started_snapshot_id TEXT NOT NULL REFERENCES following_snapshots(snapshot_id),
    ended_snapshot_id TEXT REFERENCES following_snapshots(snapshot_id),
    local_first_seen_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    platform_followed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(object_type,object_id)
        REFERENCES following_objects(object_type,object_id),
    UNIQUE(object_type,object_id,started_snapshot_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS following_relationships_one_open
ON following_relationships(object_type,object_id)
WHERE ended_snapshot_id IS NULL;
CREATE TABLE IF NOT EXISTS following_names(
    name_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    name TEXT NOT NULL,
    started_snapshot_id TEXT NOT NULL REFERENCES following_snapshots(snapshot_id),
    ended_snapshot_id TEXT REFERENCES following_snapshots(snapshot_id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY(object_type,object_id)
        REFERENCES following_objects(object_type,object_id),
    UNIQUE(object_type,object_id,started_snapshot_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS following_names_one_open
ON following_names(object_type,object_id)
WHERE ended_snapshot_id IS NULL;
CREATE TABLE IF NOT EXISTS following_changes(
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL REFERENCES following_snapshots(snapshot_id) ON DELETE CASCADE,
    change_type TEXT NOT NULL CHECK(change_type IN ('followed','unfollowed','renamed','refollowed')),
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(object_type,object_id)
        REFERENCES following_objects(object_type,object_id),
    UNIQUE(snapshot_id,change_type,object_type,object_id)
);
CREATE TABLE IF NOT EXISTS following_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    current_snapshot_id TEXT REFERENCES following_snapshots(snapshot_id)
);
INSERT OR IGNORE INTO following_state(singleton,current_snapshot_id)
VALUES (1,NULL);
"""

_EXPECTED_V1_TABLE_COLUMNS = {
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
}

_EXPECTED_TABLE_COLUMNS = {
    **_EXPECTED_V1_TABLE_COLUMNS,
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

_EXPECTED_FOREIGN_KEYS = {
    "post_revisions": [
        (0, 0, "posts", "bid", "bid", "NO ACTION", "CASCADE", "NONE")
    ],
    "comments": [
        (0, 0, "posts", "post_bid", "bid", "NO ACTION", "CASCADE", "NONE")
    ],
    "following_snapshot_items": [
        (0, 0, "following_snapshots", "snapshot_id", "snapshot_id", "NO ACTION", "CASCADE", "NONE")
    ],
    "following_relationships": [
        (0, 0, "following_objects", "object_type", "object_type", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "following_objects", "object_id", "object_id", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "following_snapshots", "ended_snapshot_id", "snapshot_id", "NO ACTION", "NO ACTION", "NONE"),
        (2, 0, "following_snapshots", "started_snapshot_id", "snapshot_id", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "following_names": [
        (0, 0, "following_objects", "object_type", "object_type", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "following_objects", "object_id", "object_id", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "following_snapshots", "ended_snapshot_id", "snapshot_id", "NO ACTION", "NO ACTION", "NONE"),
        (2, 0, "following_snapshots", "started_snapshot_id", "snapshot_id", "NO ACTION", "NO ACTION", "NONE"),
    ],
    "following_changes": [
        (0, 0, "following_objects", "object_type", "object_type", "NO ACTION", "NO ACTION", "NONE"),
        (0, 1, "following_objects", "object_id", "object_id", "NO ACTION", "NO ACTION", "NONE"),
        (1, 0, "following_snapshots", "snapshot_id", "snapshot_id", "NO ACTION", "CASCADE", "NONE"),
    ],
    "following_state": [
        (0, 0, "following_snapshots", "current_snapshot_id", "snapshot_id", "NO ACTION", "NO ACTION", "NONE")
    ],
}

_EXPECTED_UNIQUE_INDEX_COLUMNS = {
    "post_revisions": {("bid", "revision_no")},
    "media": {("owner_type", "owner_id", "role", "position")},
    "following_snapshot_items": {
        ("snapshot_id", "object_type", "object_id"),
        ("snapshot_id", "object_type", "source_order"),
    },
    "following_objects": {("object_type", "object_id")},
    "following_relationships": {
        ("object_type", "object_id", "started_snapshot_id"),
        ("object_type", "object_id"),
    },
    "following_names": {
        ("object_type", "object_id", "started_snapshot_id"),
        ("object_type", "object_id"),
    },
    "following_changes": {
        ("snapshot_id", "change_type", "object_type", "object_id"),
    },
}

_EXPECTED_PARTIAL_UNIQUE_INDEX_SQL = {
    "following_relationships_one_open": (
        "following_relationships",
        "CREATE UNIQUE INDEX following_relationships_one_open "
        "ON following_relationships(object_type,object_id) "
        "WHERE ended_snapshot_id IS NULL",
    ),
    "following_names_one_open": (
        "following_names",
        "CREATE UNIQUE INDEX following_names_one_open "
        "ON following_names(object_type,object_id) "
        "WHERE ended_snapshot_id IS NULL",
    ),
}


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(_SCHEMA_V1 + _SCHEMA_V2_ADDITIONS)
    connection.execute(
        "INSERT OR REPLACE INTO archive_meta(key, value_json) VALUES (?, ?)",
        (
            "schema_version",
            json.dumps(SCHEMA_VERSION, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.execute(
        "INSERT OR REPLACE INTO archive_meta(key, value_json) VALUES (?, ?)",
        (
            MEDIA_LAYOUT_VERSION_KEY,
            json.dumps(MEDIA_LAYOUT_VERSION, ensure_ascii=False, sort_keys=True),
        ),
    )
    connection.commit()


def read_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value_json FROM archive_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("归档数据库缺少版本记录")
    value = json.loads(row[0])
    if isinstance(value, bool) or not isinstance(value, int):
        raise sqlite3.DatabaseError("归档数据库版本记录无效")
    return value


def validate_schema_v1(connection: sqlite3.Connection) -> None:
    _validate_schema(connection, _EXPECTED_V1_TABLE_COLUMNS, {
        "post_revisions": _EXPECTED_FOREIGN_KEYS["post_revisions"],
        "comments": _EXPECTED_FOREIGN_KEYS["comments"],
    }, {
        "post_revisions": _EXPECTED_UNIQUE_INDEX_COLUMNS["post_revisions"],
        "media": _EXPECTED_UNIQUE_INDEX_COLUMNS["media"],
    })


def migrate_schema(connection: sqlite3.Connection, source_version: int) -> None:
    if source_version != 1:
        raise sqlite3.DatabaseError(f"不支持从归档版本 {source_version} 迁移")
    validate_schema_v1(connection)
    version_json = json.dumps(SCHEMA_VERSION, ensure_ascii=False, sort_keys=True)
    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + _SCHEMA_V2_ADDITIONS
            + "\nUPDATE archive_meta SET value_json = '"
            + version_json
            + "' WHERE key = 'schema_version';\nCOMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    validate_schema(connection)


def validate_schema(connection: sqlite3.Connection) -> None:
    _validate_schema(
        connection,
        _EXPECTED_TABLE_COLUMNS,
        _EXPECTED_FOREIGN_KEYS,
        _EXPECTED_UNIQUE_INDEX_COLUMNS,
    )
    for index_name, (table, expected_sql) in (
        _EXPECTED_PARTIAL_UNIQUE_INDEX_SQL.items()
    ):
        indexes = {
            row[1]: row
            for row in connection.execute(f"PRAGMA index_list({table})")
        }
        row = indexes.get(index_name)
        if row is None or row[2] != 1 or row[4] != 1:
            raise sqlite3.DatabaseError(
                f"归档数据库索引 {index_name} 条件约束不匹配"
            )
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        actual_sql = " ".join((sql_row[0] if sql_row else "").split())
        if actual_sql != expected_sql:
            raise sqlite3.DatabaseError(
                f"归档数据库索引 {index_name} 定义不匹配"
            )


def _validate_schema(
    connection: sqlite3.Connection,
    expected_table_columns: dict[str, list[tuple]],
    expected_foreign_keys: dict[str, list[tuple]],
    expected_unique_indexes: dict[str, set[tuple[str, ...]]],
) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        if not row[0].startswith("sqlite_")
    }
    if tables != set(expected_table_columns):
        raise sqlite3.DatabaseError("归档数据库表结构不完整")

    for table, expected_columns in expected_table_columns.items():
        columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if columns != expected_columns:
            raise sqlite3.DatabaseError(f"归档数据库表 {table} 列结构不匹配")

    for table, table_foreign_keys in expected_foreign_keys.items():
        foreign_keys = connection.execute(
            f"PRAGMA foreign_key_list({table})"
        ).fetchall()
        if foreign_keys != table_foreign_keys:
            raise sqlite3.DatabaseError(f"归档数据库表 {table} 外键约束不匹配")

    for table, expected_indexes in expected_unique_indexes.items():
        indexes = set()
        for _, name, unique, _, _ in connection.execute(f"PRAGMA index_list({table})"):
            if unique:
                indexes.add(
                    tuple(
                        row[2]
                        for row in connection.execute(f"PRAGMA index_info({name})")
                    )
                )
        if indexes != expected_indexes:
            raise sqlite3.DatabaseError(f"归档数据库表 {table} 唯一约束不匹配")
