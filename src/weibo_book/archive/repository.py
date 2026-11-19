"""可携带归档的 SQLite repository。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from weibo_book.errors import WeiboError

from .fingerprint import content_fingerprint
from .profile_assets import profile_asset_identity
from .video_playback import video_quality_score
from .media_layout import (
    media_path_shape,
    migrate_media_layout,
    read_media_layout_version,
)
from .paths import (
    ROOT_LAYOUT_VERSION,
    ArchivePaths,
    detect_root_layout,
    read_root_layout_version,
    write_root_layout_version,
)
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
    SCHEMA_VERSION,
    ArchiveManifest,
    CommentRecord,
    FansObservationRecord,
    MediaRecord,
    PinObservationRecord,
    PostRecord,
    PostRevisionRecord,
    ProfileAssetRecord,
    initialize_schema,
    migrate_schema,
    read_schema_version,
    validate_schema,
)


logger = logging.getLogger(__name__)

_PROFILE_ASSET_KINDS = ("avatar", "cover")


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
        paths: ArchivePaths | None = None,
        media_layout_migrated: bool = False,
    ):
        self._root = root
        self._connection = connection
        self._manifest = manifest
        self._paths = paths if paths is not None else ArchivePaths(
            root, ROOT_LAYOUT_VERSION
        )
        self._media_layout_migrated = media_layout_migrated

    @property
    def paths(self) -> ArchivePaths:
        """当前归档的根布局路径解析。"""
        return self._paths

    @property
    def media_layout_migrated(self) -> bool:
        """本次 open 是否推进了媒体布局版本。"""
        return self._media_layout_migrated

    @classmethod
    def create(cls, root: Path, uid: str, screen_name: str) -> "ArchiveRepository":
        root = Path(root)
        paths = ArchivePaths(root, ROOT_LAYOUT_VERSION)
        root_existed = root.exists()
        root_created = False
        data_dir_created = False
        connection: sqlite3.Connection | None = None
        data_dir = paths.data_dir
        database_path = paths.database_path
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
            data_dir.mkdir(parents=True)
            data_dir_created = True
            connection = sqlite3.connect(
                database_path, isolation_level=None
            )
            initialize_schema(connection)
            write_root_layout_version(connection, ROOT_LAYOUT_VERSION)
            _write_manifest_atomic(paths.manifest_path, manifest)
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
                except (FileNotFoundError, OSError):
                    pass
                try:
                    paths.assets_dir.rmdir()
                except (FileNotFoundError, OSError):
                    pass
            if root_created:
                try:
                    root.rmdir()
                except (FileNotFoundError, OSError):
                    pass
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError("创建归档失败", original=exc) from exc
        assert connection is not None
        return cls(root, connection, manifest, paths)

    @classmethod
    def open(cls, root: Path, expected_uid: str) -> "ArchiveRepository":
        root = Path(root)
        layout_version = detect_root_layout(root)
        paths = ArchivePaths(root, layout_version)
        manifest_path = paths.manifest_path
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = ArchiveManifest(**payload)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise ArchiveError("归档清单损坏，无法读取", original=exc) from exc
        if manifest.schema_version not in {1, 2, 3, SCHEMA_VERSION}:
            raise ArchiveError(
                f"归档版本不受支持：{manifest.schema_version}"
            )
        if manifest.uid != expected_uid:
            raise ArchiveIdentityError("该归档属于其他账号，已拒绝打开")

        database_path = paths.database_path
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
            if database_version in (1, 2, 3):
                migrate_schema(
                    connection,
                    database_version,
                    seed_observed_at=manifest.last_successful_sync_at,
                )
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
        media_version_before = read_media_layout_version(connection)
        try:
            if layout_version >= ROOT_LAYOUT_VERSION:
                # 根布局已是 v2：媒体布局必须随 local_path 语义一起推进到 4，
                # 失败不能保留旧布局（路径基准已变，旧形状无法解析）。
                migrate_media_layout(paths, connection)
                if read_root_layout_version(connection) != ROOT_LAYOUT_VERSION:
                    write_root_layout_version(connection, ROOT_LAYOUT_VERSION)
            else:
                migrate_media_layout(paths, connection)
        except Exception as exc:
            if layout_version >= ROOT_LAYOUT_VERSION:
                connection.close()
                raise ArchiveError("归档目录布局迁移失败", original=exc) from exc
            logger.warning("归档媒体目录迁移失败，已保留旧布局：%s", exc)
        media_layout_migrated = (
            read_media_layout_version(connection) != media_version_before
        )
        return cls(root, connection, manifest, paths, media_layout_migrated)

    def close(self) -> None:
        self._connection.close()

    def manifest(self) -> ArchiveManifest:
        return self._manifest

    def _read_meta(self, key: str) -> object | None:
        try:
            row = self._connection.execute(
                "SELECT value_json FROM archive_meta WHERE key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取归档设置失败", original=exc) from exc
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise ArchiveError("归档设置记录无效", original=exc) from exc

    def _write_meta(self, key: str, value: object) -> None:
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO archive_meta(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("写入归档设置失败", original=exc) from exc

    def read_history_cutoff(self) -> str | None:
        """已建档到的最老截止日期（YYYY-MM-DD）；缺省或 null 表示无限制。"""
        value = self._read_meta("history_cutoff")
        if value is None:
            return None
        if not isinstance(value, str):
            raise ArchiveError("归档历史截止记录无效")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ArchiveError("归档历史截止记录无效", original=exc) from exc
        if parsed.isoformat() != value:
            raise ArchiveError("归档历史截止记录无效")
        return value

    def write_history_cutoff(self, value: str | None) -> None:
        if value is not None:
            if not isinstance(value, str):
                raise ArchiveError("归档历史截止日期无效")
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ArchiveError("归档历史截止日期无效", original=exc) from exc
            if parsed.isoformat() != value:
                raise ArchiveError("归档历史截止日期无效")
        self._write_meta("history_cutoff", value)

    def read_history_complete(self) -> bool:
        """历史是否已补全；缺省 true（旧版本建档均为全量）。"""
        value = self._read_meta("history_complete")
        if value is None:
            return True
        if type(value) is not bool:
            raise ArchiveError("归档历史完整性记录无效")
        return value

    def write_history_complete(self, value: bool) -> None:
        if type(value) is not bool:
            raise ArchiveError("归档历史完整性取值无效")
        self._write_meta("history_complete", value)

    def read_output_formats(self) -> dict[str, bool]:
        """额外成品格式开关（HTML 恒生成）；缺省只出互动 HTML。"""
        value = self._read_meta("output_formats")
        if value is None:
            return {"pdf": False, "markdown": False}
        if not isinstance(value, dict) or not set(value) <= {"pdf", "markdown"}:
            raise ArchiveError("归档保存格式设置无效")
        formats = {"pdf": False, "markdown": False}
        for key, item in value.items():
            if type(item) is not bool:
                raise ArchiveError("归档保存格式设置无效")
            formats[key] = item
        return formats

    def write_output_formats(self, *, pdf: bool, markdown: bool) -> None:
        if type(pdf) is not bool or type(markdown) is not bool:
            raise ArchiveError("归档保存格式取值无效")
        self._write_meta("output_formats", {"pdf": pdf, "markdown": markdown})

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

    def record_profile_asset(
        self,
        kind: str,
        remote_url: str,
        local_path: str = "",
        sha256: str = "",
        *,
        first_seen_at: str,
        run_id: str,
    ) -> bool:
        """记录一次头像/封面观察；返回是否为新记录。

        同 kind 图片按 URL 路径文件名去重，保留最早首次观察时间。
        缺少本地文件时补全路径；不删除旧版本已经写入的重复行。
        """
        if kind not in _PROFILE_ASSET_KINDS:
            raise ArchiveError("资料媒体类型无效")
        if not isinstance(remote_url, str) or not remote_url:
            raise ArchiveError("资料媒体链接不能为空")
        if not isinstance(first_seen_at, str) or not first_seen_at:
            raise ArchiveError("资料媒体首次观察时间不能为空")
        if not isinstance(run_id, str):
            raise ArchiveError("资料媒体运行标识无效")
        if local_path and media_path_shape(local_path) is None:
            raise ArchiveError("资料媒体本地路径必须是归档媒体目录下有效的 POSIX 相对路径")
        try:
            identity = profile_asset_identity(remote_url)
            existing = next((asset for asset in reversed(self.list_profile_assets(kind))
                             if profile_asset_identity(asset.remote_url) == identity), None)
            if existing is not None:
                self._connection.execute(
                    "UPDATE profile_assets SET first_seen_at = MIN(first_seen_at, ?) "
                    "WHERE kind = ? AND remote_url = ?",
                    (first_seen_at, kind, existing.remote_url),
                )
                if local_path and not existing.local_path:
                    self._connection.execute(
                        "UPDATE profile_assets SET local_path = ?, sha256 = ? "
                        "WHERE kind = ? AND remote_url = ?",
                        (local_path, sha256, kind, existing.remote_url),
                    )
                return False
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO profile_assets(
                    kind, remote_url, local_path, sha256, first_seen_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (kind, remote_url, local_path, sha256, first_seen_at, run_id),
            )
            if cursor.rowcount == 1:
                return True
            if local_path:
                self._connection.execute(
                    """
                    UPDATE profile_assets SET local_path = ?, sha256 = ?
                    WHERE kind = ? AND remote_url = ? AND local_path = ''
                    """,
                    (local_path, sha256, kind, remote_url),
                )
            return False
        except sqlite3.Error as exc:
            raise ArchiveError("保存资料媒体记录失败", original=exc) from exc

    def set_current_profile_asset(self, kind: str, remote_url: str) -> None:
        """保存当前资料接口明确返回的图片；空字符串表示当前没有图片。"""
        if kind not in _PROFILE_ASSET_KINDS or not isinstance(remote_url, str):
            raise ArchiveError("当前资料图片记录无效")
        self._write_meta("current_profile_asset_" + kind, remote_url)

    def get_current_profile_asset(self, kind: str) -> str:
        if kind not in _PROFILE_ASSET_KINDS:
            raise ArchiveError("资料媒体类型无效")
        value = self._read_meta("current_profile_asset_" + kind)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ArchiveError("当前资料图片记录无效")
        return value

    def list_profile_assets(self, kind: str | None = None) -> list[ProfileAssetRecord]:
        """按首次观察时间倒序读取头像/封面历史记录。"""
        if kind is not None and kind not in _PROFILE_ASSET_KINDS:
            raise ArchiveError("资料媒体类型无效")
        try:
            if kind is None:
                rows = self._connection.execute(
                    "SELECT kind, remote_url, local_path, sha256, first_seen_at, run_id "
                    "FROM profile_assets ORDER BY first_seen_at DESC, id DESC"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT kind, remote_url, local_path, sha256, first_seen_at, run_id "
                    "FROM profile_assets WHERE kind = ? "
                    "ORDER BY first_seen_at DESC, id DESC",
                    (kind,),
                ).fetchall()
            return [ProfileAssetRecord(*row) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取资料媒体记录失败", original=exc) from exc

    def record_pin_observations(
        self,
        run_id: str,
        observed_at: str,
        items: list[tuple[str, int | None]],
    ) -> None:
        """整批记录一次成功同步观察到的置顶微博（可重入）。"""
        if not isinstance(run_id, str) or not run_id:
            raise ArchiveError("置顶观察运行标识不能为空")
        if not isinstance(observed_at, str) or not observed_at:
            raise ArchiveError("置顶观察时间不能为空")
        normalized: list[tuple[str, str, str, int | None]] = []
        for item in items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ArchiveError("置顶观察条目无效")
            bid, pin_order = item
            if not isinstance(bid, str) or not bid:
                raise ArchiveError("置顶观察条目无效")
            if pin_order is not None and (
                isinstance(pin_order, bool) or not isinstance(pin_order, int)
            ):
                raise ArchiveError("置顶观察条目无效")
            normalized.append((run_id, observed_at, bid, pin_order))
        if not normalized:
            return
        try:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO pin_observations(
                    run_id, observed_at, bid, pin_order
                ) VALUES (?, ?, ?, ?)
                """,
                normalized,
            )
        except sqlite3.Error as exc:
            raise ArchiveError("保存置顶观察记录失败", original=exc) from exc

    def list_pin_observations(self) -> list[PinObservationRecord]:
        """按观察时间倒序、置顶序号升序读取全部置顶观察。"""
        try:
            rows = self._connection.execute(
                "SELECT run_id, observed_at, bid, pin_order FROM pin_observations "
                "ORDER BY observed_at DESC, pin_order ASC, bid ASC"
            ).fetchall()
            return [PinObservationRecord(*row) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取置顶观察记录失败", original=exc) from exc

    def latest_pin_bids(self) -> set[str]:
        """读取最新一次观察批次里的置顶微博集合。"""
        try:
            rows = self._connection.execute(
                """
                SELECT bid FROM pin_observations
                WHERE observed_at = (
                    SELECT MAX(observed_at) FROM pin_observations
                )
                """
            ).fetchall()
            return {row[0] for row in rows}
        except sqlite3.Error as exc:
            raise ArchiveError("读取当前置顶微博失败", original=exc) from exc

    def record_fans_observation(
        self,
        run_id: str,
        observed_at: str,
        followers_count: int,
    ) -> None:
        """记录一次成功同步观察到的粉丝数（可重入，每次运行一条）。"""
        if not isinstance(run_id, str) or not run_id:
            raise ArchiveError("粉丝数观察运行标识不能为空")
        if not isinstance(observed_at, str) or not observed_at:
            raise ArchiveError("粉丝数观察时间不能为空")
        if (
            isinstance(followers_count, bool)
            or not isinstance(followers_count, int)
            or followers_count < 0
        ):
            raise ArchiveError("粉丝数观察数值无效")
        try:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO fans_observations(
                    run_id, observed_at, followers_count
                ) VALUES (?, ?, ?)
                """,
                (run_id, observed_at, followers_count),
            )
        except sqlite3.Error as exc:
            raise ArchiveError("保存粉丝数观察记录失败", original=exc) from exc

    def list_fans_observations(self) -> list[FansObservationRecord]:
        """按观察时间倒序读取全部粉丝数观察。"""
        try:
            rows = self._connection.execute(
                "SELECT run_id, observed_at, followers_count FROM fans_observations "
                "ORDER BY observed_at DESC, id DESC"
            ).fetchall()
            return [FansObservationRecord(*row) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取粉丝数观察记录失败", original=exc) from exc

    def list_posts_missing_video(self) -> list[tuple[str, str]]:
        """返回签名含视频但尚无本地视频文件的微博 ``(bid, created_at)``，新帖在前。

        用于修复性补抓：早期版本因视频 CDN 防盗链未带 Referer，视频本体
        下载全部 403（只有封面落库），且签名 URL 会过期，必须重新抓帖
        拿新 URL 后补下载。
        """
        try:
            rows = self._connection.execute(
                """
                SELECT bid, created_at FROM posts
                WHERE media_signature_json LIKE '%"type": "video"%'
                AND visibility = 'visible'
                AND NOT EXISTS (
                    SELECT 1 FROM media
                    WHERE media.owner_type = 'post'
                      AND media.owner_id = posts.bid
                      AND media.role = 'video'
                )
                ORDER BY created_at DESC, bid ASC
                """
            ).fetchall()
            return [(row[0], row[1]) for row in rows]
        except sqlite3.Error as exc:
            raise ArchiveError("读取待补抓视频微博失败", original=exc) from exc

    def list_low_quality_video_posts(self) -> list[tuple[str, str, str]]:
        """列出未按当前 PC 播放列表核验、且不足 1080p 的单视频记录。

        旧 video_ld_confirmed 没有证明 PC 查询成功，不作为本轮确认依据。
        确认与当前下载 URL 绑定；媒体改变后允许重新检查。
        """
        confirmed = self._read_meta("video_quality_checked_v2")
        confirmed = confirmed if isinstance(confirmed, dict) else {}
        try:
            rows = self._connection.execute(
                """
                SELECT posts.bid, posts.created_at, media.remote_url
                FROM posts
                JOIN media ON media.owner_type = 'post'
                          AND media.owner_id = posts.bid
                          AND media.role = 'video'
                          AND media.position = 0
                WHERE posts.visibility = 'visible'
                ORDER BY posts.created_at DESC, posts.bid ASC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ArchiveError("读取待核验视频微博失败", original=exc) from exc
        return [tuple(row) for row in rows
                if (score := video_quality_score(row[2])) != (0, 0)
                and score[0] < 1920 * 1080
                and confirmed.get(row[0]) != row[2]]

    def confirm_low_quality_video(self, bid: str) -> None:
        """PC 查询成功且无更高清版本时，记录已核验的当前下载 URL。"""
        try:
            row = self._connection.execute(
                "SELECT remote_url FROM media WHERE owner_type = 'post' "
                "AND owner_id = ? AND role = 'video' AND position = 0", (bid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取视频核验记录失败", original=exc) from exc
        if row is None:
            return
        existing = self._read_meta("video_quality_checked_v2")
        checked = dict(existing) if isinstance(existing, dict) else {}
        checked[bid] = row[0]
        self._write_meta("video_quality_checked_v2", checked)

    def list_live_posts_without_replay(self) -> list[tuple[str, str, str]]:
        """返回带直播卡片但尚未落库回放的微博 ``(bid, created_at, link_card_json)``。

        已确认无回放（直播未留回放或已下架）的微博记入 ``archive_meta``
        名单，不再重复抓取。
        """
        confirmed = self._read_meta("live_replay_unavailable")
        confirmed_set = set(confirmed) if isinstance(confirmed, list) else set()
        try:
            rows = self._connection.execute(
                """
                SELECT bid, created_at, link_card_json FROM posts
                WHERE link_card_json LIKE '%"type": "live"%'
                AND visibility = 'visible'
                AND NOT EXISTS (
                    SELECT 1 FROM media
                    WHERE media.owner_type = 'post'
                      AND media.owner_id = posts.bid
                      AND media.role = 'live_replay'
                )
                ORDER BY created_at DESC, bid ASC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ArchiveError("读取直播微博失败", original=exc) from exc
        return [tuple(row) for row in rows if row[0] not in confirmed_set]

    def confirm_live_replay_unavailable(self, bid: str) -> None:
        """把微博记入「直播无回放」确认名单，回放补抓不再触碰。"""
        existing = self._read_meta("live_replay_unavailable")
        bids = list(existing) if isinstance(existing, list) else []
        if bid not in bids:
            bids.append(bid)
            self._write_meta("live_replay_unavailable", bids)

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

    def has_comments(self, bid: str) -> bool:
        try:
            row = self._connection.execute(
                "SELECT 1 FROM comments WHERE post_bid = ? LIMIT 1", (bid,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveError("读取微博评论失败", original=exc) from exc
        return row is not None

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
            raise ArchiveError("媒体本地路径必须是归档媒体目录下有效的 POSIX 相对路径")
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
            _write_manifest_atomic(self._paths.manifest_path, updated)
        except OSError as exc:
            raise ArchiveError("更新归档成功时间失败", original=exc) from exc
        self._manifest = updated
