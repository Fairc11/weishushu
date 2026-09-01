"""可携带归档的 SQLite repository。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from weibo_book.errors import WeiboError

from .fingerprint import content_fingerprint
from .media_layout import media_path_shape, migrate_media_layout
from .following import (
    FollowingChangeRecord,
    FollowingCommitResult,
    FollowingNameRecord,
    FollowingObjectRecord,
    FollowingRelationshipRecord,
    FollowingSnapshotRecord,
)
from .schema import (
    DATABASE_NAME,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    ArchiveManifest,
    CommentRecord,
    MediaRecord,
    PostRecord,
    PostRevisionRecord,
    initialize_schema,
    migrate_schema,
    read_schema_version,
    validate_schema,
)


logger = logging.getLogger(__name__)


class ArchiveError(WeiboError):
    pass


class ArchiveIdentityError(ArchiveError):
    pass


@dataclass(frozen=True)
class PostChangeResult:
    kind: str


@dataclass(frozen=True)
class SyncRunRecord:
    run_id: str
    mode: str
    status: str
    checkpoint: dict[str, object]
    summary: dict[str, object] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _write_manifest_atomic(path: Path, manifest: ArchiveManifest) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(_json_dump(asdict(manifest)))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ArchiveRepository:
    def __init__(
        self,
        root: Path,
        connection: sqlite3.Connection,
        manifest: ArchiveManifest,
    ):
        self._root = root
        self._connection = connection
        self._manifest = manifest

    @classmethod
    def create(cls, root: Path, uid: str, screen_name: str) -> "ArchiveRepository":
        root = Path(root)
        root_existed = root.exists()
        root_created = False
        data_dir_created = False
        connection: sqlite3.Connection | None = None
        data_dir = root / "data"
        database_path = data_dir / DATABASE_NAME
        manifest = ArchiveManifest(
            schema_version=SCHEMA_VERSION,
            uid=uid,
            screen_name=screen_name,
            created_at=_utc_now(),
        )
        try:
            if root.exists() and (not root.is_dir() or any(root.iterdir())):
                raise ArchiveError("归档目录不是空目录")
            root.mkdir(parents=True, exist_ok=True)
            root_created = not root_existed
            data_dir.mkdir()
            data_dir_created = True
            connection = sqlite3.connect(
                database_path, isolation_level=None
            )
            initialize_schema(connection)
            _write_manifest_atomic(root / MANIFEST_NAME, manifest)
        except Exception as exc:
            if connection is not None:
                connection.close()
            if data_dir_created:
                for generated_file in (
                    database_path.with_name(f"{DATABASE_NAME}-shm"),
                    database_path.with_name(f"{DATABASE_NAME}-wal"),
                    database_path,
                ):
                    try:
                        generated_file.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    data_dir.rmdir()
                except FileNotFoundError:
                    pass
            if root_created:
                try:
                    root.rmdir()
                except FileNotFoundError:
                    pass
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError("创建归档失败", original=exc) from exc
        assert connection is not None
        return cls(root, connection, manifest)

    @classmethod
    def open(cls, root: Path, expected_uid: str) -> "ArchiveRepository":
        root = Path(root)
        manifest_path = root / MANIFEST_NAME
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = ArchiveManifest(**payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("归档清单损坏，无法读取", original=exc) from exc
        if manifest.schema_version not in {1, SCHEMA_VERSION}:
            raise ArchiveError(
                f"归档版本不受支持：{manifest.schema_version}"
            )
        if manifest.uid != expected_uid:
            raise ArchiveIdentityError("该归档属于其他账号，已拒绝打开")

        database_path = root / "data" / DATABASE_NAME
        if not database_path.is_file():
            raise ArchiveError("归档数据库不存在")
        try:
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise sqlite3.DatabaseError("完整性检查未通过")
            database_version = read_schema_version(connection)
            if database_version == 1:
                migrate_schema(connection, database_version)
            elif database_version == SCHEMA_VERSION:
                validate_schema(connection)
            else:
                raise sqlite3.DatabaseError(
                    f"归档数据库版本不受支持：{database_version}"
                )
            if manifest.schema_version != SCHEMA_VERSION:
                manifest = replace(manifest, schema_version=SCHEMA_VERSION)
                _write_manifest_atomic(manifest_path, manifest)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            try:
                connection.close()
            except UnboundLocalError:
                pass
            raise ArchiveError("归档数据库损坏，无法打开", original=exc) from exc
        try:
            migrate_media_layout(root, connection)
        except Exception as exc:
            logger.warning("归档媒体目录迁移失败，已保留旧布局：%s", exc)
        return cls(root, connection, manifest)

    def close(self) -> None:
        self._connection.close()

    def manifest(self) -> ArchiveManifest:
        return self._manifest

    def root_path(self) -> Path:
        """返回当前 repository 打开的精确归档根目录。"""
        return self._root

    def begin_following_snapshot(
        self,
        started_at: str,
        snapshot_id: str | None = None,
    ) -> str:
        if not started_at:
            raise ArchiveError("关注资料快照开始时间不能为空")
        if snapshot_id is None:
            snapshot_id = str(uuid.uuid4())
        else:
            try:
                if str(uuid.UUID(snapshot_id)) != snapshot_id:
                    raise ValueError("not canonical")
            except (TypeError, ValueError, AttributeError) as exc:
                raise ArchiveError("关注资料快照标识无效", original=exc) from exc
        try:
            self._connection.execute(
                "INSERT INTO following_snapshots(snapshot_id,status,started_at) "
                "VALUES (?,'staging',?)",
                (snapshot_id, started_at),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("创建关注资料暂存快照失败", original=exc) from exc
        return snapshot_id

    def following_snapshot_exists(self, snapshot_id: str) -> bool:
        if not snapshot_id:
            raise ArchiveError("关注资料快照标识不能为空")
        row = self._connection.execute(
            "SELECT 1 FROM following_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        return row is not None

    def stage_following_items(
        self, snapshot_id: str, items: list[FollowingObjectRecord]
    ) -> None:
        if not snapshot_id:
            raise ArchiveError("关注资料快照标识不能为空")
        for item in items:
            if not isinstance(item, FollowingObjectRecord):
                raise ArchiveError("关注资料暂存条目类型无效")
        try:
            with self.transaction():
                row = self._connection.execute(
                    "SELECT status FROM following_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    raise ArchiveError("关注资料暂存快照不存在")
                if row[0] != "staging":
                    raise ArchiveError("正式关注资料快照不允许改写")
                self._connection.executemany(
                    """
                    INSERT INTO following_snapshot_items(
                        snapshot_id,object_type,object_id,display_name,page_url,
                        app_scheme,source_order,platform_followed_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(snapshot_id,object_type,object_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        page_url=excluded.page_url,
                        app_scheme=excluded.app_scheme,
                        source_order=excluded.source_order,
                        platform_followed_at=excluded.platform_followed_at
                    """,
                    [
                        (
                            snapshot_id,
                            item.object_type,
                            item.object_id,
                            item.display_name,
                            item.page_url,
                            item.app_scheme,
                            item.source_order,
                            item.platform_followed_at,
                        )
                        for item in items
                    ],
                )
        except ArchiveError:
            raise
        except sqlite3.Error as exc:
            raise ArchiveError("保存关注资料暂存条目失败", original=exc) from exc

    def discard_following_snapshot(self, snapshot_id: str) -> None:
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    "DELETE FROM following_snapshots "
                    "WHERE snapshot_id = ? AND status = 'staging'",
                    (snapshot_id,),
                )
                if cursor.rowcount != 1:
                    raise ArchiveError("关注资料暂存快照不存在或已正式提交")
        except ArchiveError:
            raise
        except sqlite3.Error as exc:
            raise ArchiveError("放弃关注资料暂存快照失败", original=exc) from exc

    def commit_following_snapshot(
        self,
        snapshot_id: str,
        *,
        cutoff_at: str,
        bloggers_complete: bool,
        supertopics_complete: bool,
        blogger_reported_total: int | None,
        supertopic_reported_total: int | None,
        blogger_unconfirmed: bool = False,
    ) -> FollowingCommitResult:
        if not cutoff_at:
            raise ArchiveError("关注资料快照截止时间不能为空")
        if not bloggers_complete or not supertopics_complete:
            raise ArchiveError("只有两类清单均完整时才能提交关注资料快照")
        for value in (blogger_reported_total, supertopic_reported_total):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ArchiveError("关注资料报告总数无效")
        try:
            with self.transaction():
                snapshot = self._connection.execute(
                    "SELECT status FROM following_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if snapshot is None:
                    raise ArchiveError("关注资料暂存快照不存在")
                if snapshot[0] != "staging":
                    raise ArchiveError("关注资料快照已经正式提交")
                counts = dict(
                    self._connection.execute(
                        "SELECT object_type,COUNT(*) FROM following_snapshot_items "
                        "WHERE snapshot_id = ? GROUP BY object_type",
                        (snapshot_id,),
                    ).fetchall()
                )
                blogger_count = int(counts.get("blogger", 0))
                supertopic_count = int(counts.get("supertopic", 0))
                # 平台可能不返回部分关注条目（报告总数包含它们）。此时只要求
                # 实际条目数不超过报告总数，缺失的博主标记为「未确认」而不是
                # 取消关注；条目数多于报告总数仍然视为异常。
                blogger_count_valid = (
                    blogger_count <= blogger_reported_total
                    if blogger_unconfirmed
                    else blogger_count == blogger_reported_total
                )
                if (
                    not blogger_count_valid
                    or supertopic_count != supertopic_reported_total
                ):
                    raise ArchiveError("关注资料条目数与报告总数不一致")

                previous_snapshot_id = self._connection.execute(
                    "SELECT current_snapshot_id FROM following_state WHERE singleton = 1"
                ).fetchone()[0]
                items = self._following_items(snapshot_id)
                previous_items = (
                    {
                        (item.object_type, item.object_id): item
                        for item in self._following_items(previous_snapshot_id)
                    }
                    if previous_snapshot_id is not None
                    else {}
                )
                current_items = {
                    (item.object_type, item.object_id): item for item in items
                }
                followed_count = 0
                unfollowed_count = 0
                renamed_count = 0
                refollowed_count = 0
                unconfirmed: list[dict[str, str]] = []

                # 以「仍有效的关系」为准找缺失对象：与上一快照条目集合在既有
                # 档案中等价，同时让曾被标记未确认、后来确认消失的对象仍能在
                # 清单完整的运行中正常判定为取消关注。
                open_identities = self._connection.execute(
                    "SELECT object_type,object_id FROM following_relationships "
                    "WHERE ended_snapshot_id IS NULL"
                ).fetchall()
                for identity in (
                    (row[0], row[1]) for row in open_identities
                ):
                    if identity in current_items:
                        continue
                    previous_item = previous_items.get(identity)
                    if previous_item is not None:
                        display_name = previous_item.display_name
                        page_url = previous_item.page_url
                        app_scheme = previous_item.app_scheme
                    else:
                        stored = self._connection.execute(
                            "SELECT current_name,page_url,app_scheme "
                            "FROM following_objects "
                            "WHERE object_type=? AND object_id=?",
                            identity,
                        ).fetchone()
                        if stored is None:
                            raise ArchiveError("关注资料对象记录不完整")
                        display_name, page_url, app_scheme = stored
                    if identity[0] == "blogger" and blogger_unconfirmed:
                        unconfirmed.append({
                            "object_id": identity[1],
                            "name": display_name,
                            "page_url": page_url,
                        })
                        continue
                    missing_item = FollowingObjectRecord(
                        identity[0], identity[1], display_name, page_url,
                        app_scheme, 0,
                    )
                    self._connection.execute(
                        """
                        UPDATE following_relationships
                        SET ended_snapshot_id=?
                        WHERE object_type=? AND object_id=?
                          AND ended_snapshot_id IS NULL
                        """,
                        (snapshot_id, identity[0], identity[1]),
                    )
                    self._connection.execute(
                        """
                        UPDATE following_names
                        SET ended_snapshot_id=?
                        WHERE object_type=? AND object_id=?
                          AND ended_snapshot_id IS NULL
                        """,
                        (snapshot_id, identity[0], identity[1]),
                    )
                    self._insert_following_change(
                        snapshot_id,
                        "unfollowed",
                        missing_item,
                        before={"name": display_name},
                        after={},
                    )
                    unfollowed_count += 1

                for item in items:
                    identity = (item.object_type, item.object_id)
                    self._connection.execute(
                        """
                        INSERT INTO following_objects(
                            object_type,object_id,current_name,page_url,app_scheme,
                            first_seen_at,last_seen_at
                        ) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(object_type,object_id) DO UPDATE SET
                            current_name=excluded.current_name,
                            page_url=excluded.page_url,
                            app_scheme=excluded.app_scheme,
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            item.object_type,
                            item.object_id,
                            item.display_name,
                            item.page_url,
                            item.app_scheme,
                            cutoff_at,
                            cutoff_at,
                        ),
                    )
                    open_relationship = self._connection.execute(
                        """
                        SELECT relationship_id,platform_followed_at
                        FROM following_relationships
                        WHERE object_type=? AND object_id=?
                          AND ended_snapshot_id IS NULL
                        """,
                        identity,
                    ).fetchone()
                    if open_relationship is None:
                        had_relationship = self._connection.execute(
                            "SELECT 1 FROM following_relationships "
                            "WHERE object_type=? AND object_id=? LIMIT 1",
                            identity,
                        ).fetchone() is not None
                        self._connection.execute(
                            """
                            INSERT INTO following_relationships(
                                object_type,object_id,started_snapshot_id,
                                local_first_seen_at,last_confirmed_at,
                                platform_followed_at
                            ) VALUES (?,?,?,?,?,?)
                            """,
                            (
                                item.object_type,
                                item.object_id,
                                snapshot_id,
                                cutoff_at,
                                cutoff_at,
                                item.platform_followed_at,
                            ),
                        )
                        if previous_snapshot_id is not None:
                            change_type = "refollowed" if had_relationship else "followed"
                            self._insert_following_change(
                                snapshot_id,
                                change_type,
                                item,
                                before={},
                                after={"name": item.display_name},
                            )
                            if had_relationship:
                                refollowed_count += 1
                            else:
                                followed_count += 1
                    else:
                        platform_followed_at = (
                            open_relationship[1] or item.platform_followed_at
                        )
                        self._connection.execute(
                            """
                            UPDATE following_relationships
                            SET last_confirmed_at=?,platform_followed_at=?
                            WHERE relationship_id=?
                            """,
                            (cutoff_at, platform_followed_at, open_relationship[0]),
                        )

                    open_name = self._connection.execute(
                        """
                        SELECT name_record_id,name FROM following_names
                        WHERE object_type=? AND object_id=?
                          AND ended_snapshot_id IS NULL
                        """,
                        identity,
                    ).fetchone()
                    if open_name is None:
                        self._connection.execute(
                            """
                            INSERT INTO following_names(
                                object_type,object_id,name,started_snapshot_id,
                                first_seen_at,last_seen_at
                            ) VALUES (?,?,?,?,?,?)
                            """,
                            (
                                item.object_type,
                                item.object_id,
                                item.display_name,
                                snapshot_id,
                                cutoff_at,
                                cutoff_at,
                            ),
                        )
                    elif open_name[1] == item.display_name:
                        self._connection.execute(
                            "UPDATE following_names SET last_seen_at=? "
                            "WHERE name_record_id=?",
                            (cutoff_at, open_name[0]),
                        )
                    else:
                        self._connection.execute(
                            "UPDATE following_names SET ended_snapshot_id=? "
                            "WHERE name_record_id=?",
                            (snapshot_id, open_name[0]),
                        )
                        self._connection.execute(
                            """
                            INSERT INTO following_names(
                                object_type,object_id,name,started_snapshot_id,
                                first_seen_at,last_seen_at
                            ) VALUES (?,?,?,?,?,?)
                            """,
                            (
                                item.object_type,
                                item.object_id,
                                item.display_name,
                                snapshot_id,
                                cutoff_at,
                                cutoff_at,
                            ),
                        )
                        self._insert_following_change(
                            snapshot_id,
                            "renamed",
                            item,
                            before={"name": open_name[1]},
                            after={"name": item.display_name},
                        )
                        renamed_count += 1

                summary = {
                    "blogger_count": blogger_count,
                    "supertopic_count": supertopic_count,
                    "followed_count": followed_count,
                    "unfollowed_count": unfollowed_count,
                    "renamed_count": renamed_count,
                    "refollowed_count": refollowed_count,
                    "unconfirmed_count": len(unconfirmed),
                }
                if unconfirmed:
                    summary["blogger_unconfirmed"] = True
                    summary["unconfirmed_bloggers"] = unconfirmed
                completed_at = _utc_now()
                self._connection.execute(
                    """
                    UPDATE following_snapshots
                    SET status='complete',cutoff_at=?,bloggers_complete=1,
                        supertopics_complete=1,blogger_reported_total=?,
                        supertopic_reported_total=?,completed_at=?,summary_json=?
                    WHERE snapshot_id=? AND status='staging'
                    """,
                    (
                        cutoff_at,
                        blogger_reported_total,
                        supertopic_reported_total,
                        completed_at,
                        _json_dump(summary),
                        snapshot_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE following_state SET current_snapshot_id=? WHERE singleton=1",
                    (snapshot_id,),
                )
        except ArchiveError:
            raise
        except sqlite3.Error as exc:
            raise ArchiveError("提交关注资料快照失败", original=exc) from exc
        return FollowingCommitResult(
            snapshot_id=snapshot_id,
            initial=previous_snapshot_id is None,
            blogger_count=blogger_count,
            supertopic_count=supertopic_count,
            followed_count=followed_count,
            unfollowed_count=unfollowed_count,
            renamed_count=renamed_count,
            refollowed_count=refollowed_count,
            unconfirmed_count=len(unconfirmed),
        )

    def _insert_following_change(
        self,
        snapshot_id: str,
        change_type: str,
        item: FollowingObjectRecord,
        *,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO following_changes(
                snapshot_id,change_type,object_type,object_id,
                before_json,after_json
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                change_type,
                item.object_type,
                item.object_id,
                _json_dump(before),
                _json_dump(after),
            ),
        )

    def get_current_following_snapshot(self) -> FollowingSnapshotRecord | None:
        row = self._connection.execute(
            """
            SELECT s.snapshot_id,s.status,s.started_at,s.cutoff_at,
                   s.bloggers_complete,s.supertopics_complete,
                   s.blogger_reported_total,s.supertopic_reported_total,
                   s.completed_at,s.summary_json
            FROM following_state state
            LEFT JOIN following_snapshots s
              ON s.snapshot_id=state.current_snapshot_id
            WHERE state.singleton=1
            """
        ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            return FollowingSnapshotRecord(
                row[0], row[1], row[2], row[3], bool(row[4]), bool(row[5]),
                row[6], row[7], row[8], json.loads(row[9]),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("关注资料快照数据损坏", original=exc) from exc

    def get_following_snapshot(self, snapshot_id: str) -> FollowingSnapshotRecord:
        row = self._connection.execute(
            """
            SELECT snapshot_id,status,started_at,cutoff_at,bloggers_complete,
                   supertopics_complete,blogger_reported_total,
                   supertopic_reported_total,completed_at,summary_json
            FROM following_snapshots WHERE snapshot_id=?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ArchiveError("关注资料快照不存在")
        try:
            return FollowingSnapshotRecord(
                row[0], row[1], row[2], row[3], bool(row[4]), bool(row[5]),
                row[6], row[7], row[8], json.loads(row[9]),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("关注资料快照数据损坏", original=exc) from exc

    def is_initial_following_snapshot(self, snapshot_id: str) -> bool:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM following_snapshots "
            "WHERE status='complete' AND snapshot_id<>?",
            (snapshot_id,),
        ).fetchone()
        return row is not None and int(row[0]) == 0

    def _following_items(self, snapshot_id: str) -> list[FollowingObjectRecord]:
        rows = self._connection.execute(
            """
            SELECT object_type,object_id,display_name,page_url,app_scheme,
                   source_order,platform_followed_at
            FROM following_snapshot_items
            WHERE snapshot_id=?
            ORDER BY object_type,source_order,object_id
            """,
            (snapshot_id,),
        ).fetchall()
        return [FollowingObjectRecord(*row) for row in rows]

    def list_following_snapshot_items(
        self, snapshot_id: str | None = None
    ) -> list[FollowingObjectRecord]:
        if snapshot_id is None:
            current = self.get_current_following_snapshot()
            if current is None:
                return []
            snapshot_id = current.snapshot_id
        return self._following_items(snapshot_id)

    def list_following_relationships(self) -> list[FollowingRelationshipRecord]:
        rows = self._connection.execute(
            """
            SELECT relationship_id,object_type,object_id,started_snapshot_id,
                   ended_snapshot_id,local_first_seen_at,last_confirmed_at,
                   platform_followed_at
            FROM following_relationships ORDER BY relationship_id
            """
        ).fetchall()
        return [FollowingRelationshipRecord(*row) for row in rows]

    def list_following_names(self) -> list[FollowingNameRecord]:
        rows = self._connection.execute(
            """
            SELECT name_record_id,object_type,object_id,name,started_snapshot_id,
                   ended_snapshot_id,first_seen_at,last_seen_at
            FROM following_names ORDER BY name_record_id
            """
        ).fetchall()
        return [FollowingNameRecord(*row) for row in rows]

    def list_following_changes(self) -> list[FollowingChangeRecord]:
        rows = self._connection.execute(
            """
            SELECT change_id,snapshot_id,change_type,object_type,object_id,
                   before_json,after_json
            FROM following_changes ORDER BY change_id
            """
        ).fetchall()
        try:
            return [
                FollowingChangeRecord(
                    row[0], row[1], row[2], row[3], row[4],
                    json.loads(row[5]), json.loads(row[6]),
                )
                for row in rows
            ]
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("关注资料变化索引损坏", original=exc) from exc

    def transaction(self) -> AbstractContextManager[None]:
        return self._transaction()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        nested = self._connection.in_transaction
        savepoint = f"archive_{uuid.uuid4().hex}"
        try:
            if nested:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            else:
                self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise ArchiveError("无法开始归档事务", original=exc) from exc
        try:
            yield
        except sqlite3.Error as exc:
            self._rollback_transaction(nested, savepoint)
            raise ArchiveError("归档数据库操作失败", original=exc) from exc
        except BaseException:
            self._rollback_transaction(nested, savepoint)
            raise
        else:
            try:
                if nested:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._connection.commit()
            except sqlite3.Error as exc:
                self._rollback_transaction(nested, savepoint)
                raise ArchiveError("提交归档事务失败", original=exc) from exc

    def _rollback_transaction(self, nested: bool, savepoint: str) -> None:
        if nested:
            self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            self._connection.rollback()

    def get_post(self, bid: str) -> PostRecord | None:
        try:
            row = self._connection.execute(
                """
                SELECT bid, uid, text, created_at, source, ip_location, is_pinned,
                       pin_order, visibility, reposts_count, comments_count,
                       likes_count, retweeted_json, link_card_json,
                       media_signature_json
                FROM posts WHERE bid = ?
                """,
                (bid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取微博记录失败", original=exc) from exc
        if row is None:
            return None
        return self._post_from_row(row)

    def list_known_bids(self) -> set[str]:
        try:
            return {row[0] for row in self._connection.execute("SELECT bid FROM posts")}
        except sqlite3.Error as exc:
            raise ArchiveError("读取微博索引失败", original=exc) from exc

    def list_pinned_bids(self) -> list[str]:
        try:
            return [row[0] for row in self._connection.execute(
                "SELECT bid FROM posts WHERE is_pinned = 1 ORDER BY pin_order, bid"
            )]
        except sqlite3.Error as exc:
            raise ArchiveError("读取置顶微博失败", original=exc) from exc

    def list_posts_for_render(self) -> list[PostRecord]:
        """按数据库稳定顺序读取当前微博，不写数据库。"""
        try:
            rows = self._connection.execute(
                """
                SELECT bid, uid, text, created_at, source, ip_location, is_pinned,
                       pin_order, visibility, reposts_count, comments_count,
                       likes_count, retweeted_json, link_card_json,
                       media_signature_json
                FROM posts ORDER BY rowid
                """
            ).fetchall()
            return [self._post_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取微博渲染快照失败", original=exc) from exc

    @staticmethod
    def _post_from_row(row: tuple) -> PostRecord:
        try:
            return PostRecord(
                bid=row[0], uid=row[1], text=row[2], created_at=row[3],
                source=row[4], ip_location=row[5], is_pinned=bool(row[6]),
                pin_order=row[7], visibility=row[8], reposts_count=row[9],
                comments_count=row[10], likes_count=row[11],
                retweeted_payload=json.loads(row[12]) if row[12] is not None else None,
                link_card_payload=json.loads(row[13]) if row[13] is not None else None,
                media_signature=json.loads(row[14]),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("微博记录数据损坏", original=exc) from exc

    def list_comments_for_render(self) -> list[CommentRecord]:
        """读取当前评论及其精确 parent_id。"""
        try:
            rows = self._connection.execute(
                "SELECT id, post_bid, parent_id, payload_json, captured_at "
                "FROM comments ORDER BY rowid"
            ).fetchall()
            return [
                CommentRecord(row[0], row[1], row[2], json.loads(row[3]), row[4])
                for row in rows
            ]
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("读取评论渲染快照失败", original=exc) from exc

    def list_media_for_render(self) -> list[MediaRecord]:
        """读取已安装的本地媒体记录；不返回远程内容。"""
        try:
            rows = self._connection.execute(
                "SELECT owner_type, owner_id, role, position, remote_url, local_path, sha256 "
                "FROM media ORDER BY rowid"
            ).fetchall()
            return [MediaRecord(*row) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取媒体渲染快照失败", original=exc) from exc

    def upsert_post(self, post: PostRecord) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO posts(
                    bid, uid, text, created_at, source, ip_location, is_pinned,
                    pin_order, visibility, reposts_count, comments_count,
                    likes_count, retweeted_json, link_card_json,
                    media_signature_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bid) DO UPDATE SET
                    uid=excluded.uid,
                    text=excluded.text,
                    created_at=excluded.created_at,
                    source=excluded.source,
                    ip_location=excluded.ip_location,
                    is_pinned=excluded.is_pinned,
                    pin_order=excluded.pin_order,
                    visibility=excluded.visibility,
                    reposts_count=excluded.reposts_count,
                    comments_count=excluded.comments_count,
                    likes_count=excluded.likes_count,
                    retweeted_json=excluded.retweeted_json,
                    link_card_json=excluded.link_card_json,
                    media_signature_json=excluded.media_signature_json
                """,
                (
                    post.bid,
                    post.uid,
                    post.text,
                    post.created_at,
                    post.source,
                    post.ip_location,
                    int(post.is_pinned),
                    post.pin_order,
                    post.visibility,
                    post.reposts_count,
                    post.comments_count,
                    post.likes_count,
                    _json_dump(post.retweeted_payload)
                    if post.retweeted_payload is not None
                    else None,
                    _json_dump(post.link_card_payload)
                    if post.link_card_payload is not None
                    else None,
                    _json_dump(post.media_signature),
                ),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("保存微博记录失败", original=exc) from exc

    def add_post_revision(self, revision: PostRevisionRecord) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO post_revisions(
                    bid, revision_no, captured_at, payload_json, content_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revision.bid,
                    revision.revision_no,
                    revision.captured_at,
                    _json_dump(revision.payload),
                    revision.content_hash,
                ),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("保存微博修订记录失败", original=exc) from exc

    def list_revisions(self, bid: str) -> list[PostRevisionRecord]:
        try:
            rows = self._connection.execute(
                """
                SELECT bid, revision_no, captured_at, payload_json, content_hash
                FROM post_revisions
                WHERE bid = ?
                ORDER BY revision_no
                """,
                (bid,),
            ).fetchall()
            return [
                PostRevisionRecord(
                    bid=row[0],
                    revision_no=row[1],
                    captured_at=row[2],
                    payload=json.loads(row[3]),
                    content_hash=row[4],
                )
                for row in rows
            ]
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("读取微博修订记录失败", original=exc) from exc

    def list_revisions_for_render(self) -> dict[str, list[PostRevisionRecord]]:
        """单次读取全部修订，避免渲染阶段按微博逐条查询。"""
        try:
            rows = self._connection.execute(
                "SELECT bid, revision_no, captured_at, payload_json, content_hash "
                "FROM post_revisions ORDER BY bid, revision_no"
            ).fetchall()
            result: dict[str, list[PostRevisionRecord]] = {}
            for row in rows:
                result.setdefault(row[0], []).append(PostRevisionRecord(
                    bid=row[0], revision_no=row[1], captured_at=row[2],
                    payload=json.loads(row[3]), content_hash=row[4],
                ))
            return result
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("读取微博修订渲染快照失败", original=exc) from exc

    def apply_post_change(self, post: PostRecord) -> PostChangeResult:
        with self.transaction():
            current = self.get_post(post.bid)
            if current is None:
                self.upsert_post(post)
                return PostChangeResult(kind="new")

            if current == post:
                return PostChangeResult(kind="unchanged")

            if content_fingerprint(current) == content_fingerprint(post):
                self.upsert_post(post)
                return PostChangeResult(kind="counts_changed")

            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(revision_no), 0) + 1
                FROM post_revisions
                WHERE bid = ?
                """,
                (post.bid,),
            ).fetchone()
            revision_no = row[0]
            self.add_post_revision(
                PostRevisionRecord(
                    bid=current.bid,
                    revision_no=revision_no,
                    captured_at=_utc_now(),
                    payload=asdict(current),
                    content_hash=content_fingerprint(current),
                )
            )
            self.upsert_post(post)
            return PostChangeResult(kind="content_changed")

    def replace_current_comments(
        self, bid: str, comments: list[CommentRecord]
    ) -> None:
        try:
            with self.transaction():
                self._connection.execute("DELETE FROM comments WHERE post_bid = ?", (bid,))
                self._connection.executemany(
                    """
                    INSERT INTO comments(
                        id, post_bid, parent_id, payload_json, captured_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            comment.id,
                            comment.post_bid,
                            comment.parent_id,
                            _json_dump(comment.payload),
                            comment.captured_at,
                        )
                        for comment in comments
                    ],
                )
        except ArchiveError:
            raise
        except sqlite3.Error as exc:
            raise ArchiveError("替换微博评论失败", original=exc) from exc

    def upsert_media(self, media: MediaRecord) -> None:
        path = media.local_path
        if media_path_shape(path) is None:
            raise ArchiveError("媒体本地路径必须是 media/ 下有效的 POSIX 相对路径")
        try:
            self._connection.execute(
                """
                INSERT INTO media(
                    owner_type, owner_id, role, position, remote_url,
                    local_path, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_type, owner_id, role, position) DO UPDATE SET
                    remote_url=excluded.remote_url,
                    local_path=excluded.local_path,
                    sha256=excluded.sha256
                """,
                (
                    media.owner_type,
                    media.owner_id,
                    media.role,
                    media.position,
                    media.remote_url,
                    media.local_path,
                    media.sha256,
                ),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("保存媒体记录失败", original=exc) from exc

    def begin_sync(self, mode: str) -> str:
        run_id = str(uuid.uuid4())
        try:
            self._connection.execute(
                """
                INSERT INTO sync_runs(run_id, mode, status, started_at)
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, mode, _utc_now()),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("创建同步记录失败", original=exc) from exc
        return run_id

    def finish_sync(
        self, run_id: str, status: str, summary: dict[str, object]
    ) -> None:
        try:
            cursor = self._connection.execute(
                """
                UPDATE sync_runs
                SET status = ?, finished_at = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (status, _utc_now(), _json_dump(summary), run_id),
            )
            if cursor.rowcount != 1:
                raise ArchiveError("同步记录不存在，无法完成同步")
        except sqlite3.Error as exc:
            raise ArchiveError("更新同步记录失败", original=exc) from exc

    def mark_sync_committing(
        self,
        run_id: str,
        checkpoint: dict[str, object],
        summary: dict[str, object],
    ) -> None:
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'committing', finished_at = '',
                        checkpoint_json = ?, summary_json = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (_json_dump(checkpoint), _json_dump(summary), run_id),
                )
                if cursor.rowcount != 1:
                    raise ArchiveError("同步记录状态无法进入提交阶段")
        except sqlite3.Error as exc:
            raise ArchiveError("保存同步提交状态失败", original=exc) from exc

    def get_latest_committing_sync(self) -> SyncRunRecord | None:
        try:
            row = self._connection.execute(
                """
                SELECT run_id, mode, status, checkpoint_json, summary_json
                FROM sync_runs
                WHERE status = 'committing'
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取待完成的同步记录失败", original=exc) from exc
        if row is None:
            return None
        try:
            checkpoint = json.loads(row[3])
            summary = json.loads(row[4])
            if not isinstance(checkpoint, dict) or not isinstance(summary, dict):
                raise TypeError("提交状态不是 JSON 对象")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("同步提交状态损坏", original=exc) from exc
        return SyncRunRecord(row[0], row[1], row[2], checkpoint, summary)

    def update_sync_checkpoint(
        self, run_id: str, checkpoint: dict[str, object]
    ) -> None:
        try:
            cursor = self._connection.execute(
                "UPDATE sync_runs SET checkpoint_json = ? WHERE run_id = ?",
                (_json_dump(checkpoint), run_id),
            )
            if cursor.rowcount != 1:
                raise ArchiveError("同步记录不存在，无法保存恢复点")
        except sqlite3.Error as exc:
            raise ArchiveError("保存同步恢复点失败", original=exc) from exc

    def get_unfinished_sync(self, mode: str) -> SyncRunRecord | None:
        try:
            row = self._connection.execute(
                """
                SELECT run_id, mode, status, checkpoint_json
                FROM sync_runs
                WHERE mode = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (mode,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取同步恢复点失败", original=exc) from exc
        if row is None:
            return None
        if row[2] not in {"running", "paused", "error"}:
            return None
        try:
            checkpoint = json.loads(row[3])
            if not isinstance(checkpoint, dict):
                raise TypeError("恢复点不是 JSON 对象")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("同步恢复点损坏", original=exc) from exc
        return SyncRunRecord(row[0], row[1], row[2], checkpoint)

    def get_sync_run(self, run_id: str) -> SyncRunRecord:
        try:
            row = self._connection.execute(
                """
                SELECT run_id, mode, status, checkpoint_json, summary_json
                FROM sync_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取指定同步记录失败", original=exc) from exc
        if row is None:
            raise ArchiveError("指定同步记录不存在")
        try:
            checkpoint = json.loads(row[3])
            summary = json.loads(row[4])
            if not isinstance(checkpoint, dict) or not isinstance(summary, dict):
                raise TypeError("同步记录 JSON 类型无效")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("指定同步记录已损坏", original=exc) from exc
        return SyncRunRecord(row[0], row[1], row[2], checkpoint, summary)

    def clear_sync_checkpoint(self, run_id: str, status: str) -> None:
        if status not in {"cancelled", "abandoned"}:
            raise ArchiveError("清理同步记录的终态无效")
        try:
            with self.transaction():
                cursor = self._connection.execute(
                    """
                    UPDATE sync_runs
                    SET status = ?, finished_at = ?, checkpoint_json = '{}'
                    WHERE run_id = ?
                      AND status IN ('running', 'paused', 'error', 'cancelled')
                    """,
                    (status, _utc_now(), run_id),
                )
                if cursor.rowcount != 1:
                    raise ArchiveError("指定同步记录不允许清理")
        except sqlite3.Error as exc:
            raise ArchiveError("清理同步恢复点失败", original=exc) from exc

    def get_latest_sync_status(self, mode: str) -> tuple[str, str] | None:
        try:
            row = self._connection.execute(
                """
                SELECT run_id, status
                FROM sync_runs
                WHERE mode = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (mode,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取最新同步状态失败", original=exc) from exc
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def update_manifest_success(self, successful_at: str) -> None:
        updated = replace(self._manifest, last_successful_sync_at=successful_at)
        try:
            _write_manifest_atomic(self._root / MANIFEST_NAME, updated)
        except OSError as exc:
            raise ArchiveError("更新归档成功时间失败", original=exc) from exc
        self._manifest = updated
