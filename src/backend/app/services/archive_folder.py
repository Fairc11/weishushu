"""本人微博书目录的纯只读识别。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.schemas import ArchiveFolderInspection
from backend.app.services.backup_index import INDEX_FILENAME
from weibo_book.archive.schema import (
    DATABASE_NAME,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    read_schema_version,
    validate_schema,
    validate_schema_v1,
)
from weibo_book.errors import WeiboError, WeiboErrorKind


_MANIFEST_FIELDS = {
    "schema_version": int,
    "uid": str,
    "screen_name": str,
    "created_at": str,
    "last_successful_sync_at": str,
}
_SNAPSHOT_ATTEMPTS = 3
_SUPPORTS_DIRECTORY_FDS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)
_OPEN_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class _IntegrityCheckFailed(sqlite3.DatabaseError):
    pass


class _ArchiveSymlinkError(OSError):
    pass


class _ArchiveMarkerTypeError(OSError):
    pass


class _SnapshotChanged(OSError):
    pass


@dataclass(frozen=True)
class _OpenedSource:
    directory_fd: int
    name: str
    fd: int
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _CopiedPathSource:
    path: Path
    identity: tuple[int, int, int, int]


def _result(
    state: str,
    path: Path,
    *,
    uid: str = "",
    screen_name: str = "",
    total_posts: int = 0,
    last_successful_sync_at: str = "",
    message: str = "",
) -> ArchiveFolderInspection:
    return ArchiveFolderInspection(
        state=state,
        path=str(path),
        uid=uid,
        screen_name=screen_name,
        total_posts=total_posts,
        last_successful_sync_at=last_successful_sync_at,
        message=message,
    )


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _lstat_at(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_directory_at(directory_fd: int, name: str) -> int:
    marker = _lstat_at(directory_fd, name)
    if marker is None:
        raise FileNotFoundError(name)
    if stat.S_ISLNK(marker.st_mode):
        raise _ArchiveSymlinkError(name)
    if not stat.S_ISDIR(marker.st_mode):
        raise _ArchiveMarkerTypeError(name)
    try:
        opened = os.open(name, _OPEN_DIRECTORY_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise _SnapshotChanged(name) from exc
    except OSError as exc:
        current = _lstat_at(directory_fd, name)
        if current is None or _identity(current) != _identity(marker):
            raise _SnapshotChanged(name) from exc
        raise
    if _identity(os.fstat(opened)) != _identity(marker):
        os.close(opened)
        raise _SnapshotChanged(name)
    return opened


def _open_regular_at(directory_fd: int, name: str) -> _OpenedSource:
    marker = _lstat_at(directory_fd, name)
    if marker is None:
        raise FileNotFoundError(name)
    if stat.S_ISLNK(marker.st_mode):
        raise _ArchiveSymlinkError(name)
    if not stat.S_ISREG(marker.st_mode):
        raise _ArchiveMarkerTypeError(name)
    try:
        opened = os.open(name, _OPEN_FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise _SnapshotChanged(name) from exc
    except OSError as exc:
        current = _lstat_at(directory_fd, name)
        if current is None or _identity(current) != _identity(marker):
            raise _SnapshotChanged(name) from exc
        raise
    opened_identity = _identity(os.fstat(opened))
    if opened_identity != _identity(marker):
        os.close(opened)
        raise _SnapshotChanged(name)
    return _OpenedSource(directory_fd, name, opened, opened_identity)


def _copy_fd(source_fd: int, destination: Path) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(source_fd), "rb") as source:
        with destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _read_fd(source_fd: int) -> bytes:
    os.lseek(source_fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _source_is_unchanged(source: _OpenedSource) -> bool:
    try:
        descriptor_identity = _identity(os.fstat(source.fd))
        path_stat = _lstat_at(source.directory_fd, source.name)
    except OSError:
        return False
    return (
        descriptor_identity == source.identity
        and path_stat is not None
        and not stat.S_ISLNK(path_stat.st_mode)
        and _identity(path_stat) == source.identity
    )


def _lstat_path(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _path_root_is_unchanged(
    selected: Path,
    root_identity: tuple[int, int, int, int],
) -> bool:
    marker = _lstat_path(selected)
    return (
        marker is not None
        and not stat.S_ISLNK(marker.st_mode)
        and _identity(marker) == root_identity
    )


def _open_path_source(path: Path) -> tuple[int, _CopiedPathSource]:
    marker = _lstat_path(path)
    if marker is None:
        raise FileNotFoundError(path.name)
    if stat.S_ISLNK(marker.st_mode):
        raise _ArchiveSymlinkError(path.name)
    if not stat.S_ISREG(marker.st_mode):
        raise _ArchiveMarkerTypeError(path.name)
    try:
        descriptor = os.open(path, _OPEN_FILE_FLAGS)
    except FileNotFoundError as exc:
        raise _SnapshotChanged(path.name) from exc
    except OSError as exc:
        current = _lstat_path(path)
        if current is None or _identity(current) != _identity(marker):
            raise _SnapshotChanged(path.name) from exc
        raise
    source = _CopiedPathSource(path=path, identity=_identity(os.fstat(descriptor)))
    if source.identity != _identity(marker):
        os.close(descriptor)
        raise _SnapshotChanged(path.name)
    return descriptor, source


def _path_source_is_unchanged(source: _CopiedPathSource) -> bool:
    try:
        marker = _lstat_path(source.path)
        return (
            marker is not None
            and not stat.S_ISLNK(marker.st_mode)
            and _identity(marker) == source.identity
        )
    except OSError:
        return False


def _read_path_source(path: Path) -> tuple[bytes, _CopiedPathSource]:
    descriptor, source = _open_path_source(path)
    try:
        payload = _read_fd(descriptor)
    finally:
        os.close(descriptor)
    if not _path_source_is_unchanged(source):
        raise _SnapshotChanged(path.name)
    return payload, source


def _copy_path_source(path: Path, destination: Path) -> _CopiedPathSource:
    descriptor, source = _open_path_source(path)
    try:
        _copy_fd(descriptor, destination)
    finally:
        os.close(descriptor)
    if not _path_source_is_unchanged(source):
        raise _SnapshotChanged(path.name)
    return source


def _path_snapshot_unchanged(
    sources: tuple[_CopiedPathSource, ...],
    selected: Path,
    root_identity: tuple[int, int, int, int],
    data_path: Path,
    data_identity: tuple[int, int, int, int],
    wal_path: Path,
    wal_was_absent: bool,
) -> bool:
    root_marker = _lstat_path(selected)
    data_marker = _lstat_path(data_path)
    if (
        root_marker is None
        or stat.S_ISLNK(root_marker.st_mode)
        or _identity(root_marker) != root_identity
        or data_marker is None
        or stat.S_ISLNK(data_marker.st_mode)
        or _identity(data_marker) != data_identity
    ):
        return False
    if not all(_path_source_is_unchanged(source) for source in sources):
        return False
    return not wal_was_absent or _lstat_path(wal_path) is None


def _snapshot_source_unchanged(
    sources: tuple[_OpenedSource, ...],
    root_fd: int,
    data_fd: int,
    data_identity: tuple[int, int, int, int],
    wal_was_absent: bool,
    *,
    selected: Path | None = None,
    root_identity: tuple[int, int, int, int] | None = None,
) -> bool:
    if not all(_source_is_unchanged(source) for source in sources):
        return False
    try:
        data_path_stat = _lstat_at(root_fd, "data")
        if (
            _identity(os.fstat(data_fd)) != data_identity
            or data_path_stat is None
            or stat.S_ISLNK(data_path_stat.st_mode)
            or _identity(data_path_stat) != data_identity
        ):
            return False
    except OSError:
        return False
    if selected is not None and root_identity is not None:
        try:
            selected_stat = os.lstat(selected)
            if (
                stat.S_ISLNK(selected_stat.st_mode)
                or _identity(os.fstat(root_fd)) != root_identity
                or _identity(selected_stat) != root_identity
            ):
                return False
        except OSError:
            return False
    if wal_was_absent and _lstat_at(data_fd, f"{DATABASE_NAME}-wal") is not None:
        return False
    return True


def _close_sources(sources: tuple[_OpenedSource, ...]) -> None:
    for source in sources:
        os.close(source.fd)


def _copy_snapshot_once(
    root_fd: int,
    data_fd: int,
    temporary_root: Path,
) -> tuple[bytes, Path, tuple[_OpenedSource, ...], bool]:
    opened: list[_OpenedSource] = []
    try:
        manifest = _open_regular_at(root_fd, MANIFEST_NAME)
        opened.append(manifest)
        database = _open_regular_at(data_fd, DATABASE_NAME)
        opened.append(database)
        wal_stat = _lstat_at(data_fd, f"{DATABASE_NAME}-wal")
        if wal_stat is not None:
            opened.append(_open_regular_at(data_fd, f"{DATABASE_NAME}-wal"))

        manifest_bytes = _read_fd(manifest.fd)
        snapshot_database = temporary_root / DATABASE_NAME
        _copy_fd(database.fd, snapshot_database)
        if wal_stat is not None:
            _copy_fd(opened[-1].fd, temporary_root / f"{DATABASE_NAME}-wal")
        return manifest_bytes, snapshot_database, tuple(opened), wal_stat is None
    except FileNotFoundError as exc:
        _close_sources(tuple(opened))
        raise _SnapshotChanged("归档文件在打开前已变化") from exc
    except BaseException:
        _close_sources(tuple(opened))
        raise


def _read_manifest(payload_bytes: bytes) -> dict:
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != set(_MANIFEST_FIELDS):
        raise ValueError("归档清单字段不完整")
    for field, expected_type in _MANIFEST_FIELDS.items():
        value = payload[field]
        if expected_type is int:
            valid = type(value) is int
        else:
            valid = isinstance(value, expected_type)
        if not valid:
            raise ValueError(f"归档清单字段 {field} 类型错误")
    if payload["schema_version"] not in {1, SCHEMA_VERSION}:
        raise ValueError("归档清单版本不受支持")
    return payload


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    # 这里打开的是归档外的临时副本；SQLite 可在副本中恢复 WAL。
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _validate_database(database_path: Path) -> tuple[sqlite3.Connection, int]:
    connection = _connect_read_only(database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise _IntegrityCheckFailed("完整性检查未通过")
        schema_version = read_schema_version(connection)
        if schema_version == 1:
            validate_schema_v1(connection)
        elif schema_version == SCHEMA_VERSION:
            validate_schema(connection)
        else:
            raise sqlite3.DatabaseError("归档数据库版本不受支持")
        total_posts = connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        return connection, total_posts
    except BaseException:
        connection.close()
        raise


def _damaged_for_marker_error(
    selected: Path, error: BaseException
) -> ArchiveFolderInspection:
    if isinstance(error, _ArchiveSymlinkError):
        return _result(
            "damaged", selected, message="归档标记不能是符号链接"
        )
    if isinstance(error, _ArchiveMarkerTypeError):
        return _result("damaged", selected, message="归档标记类型错误")
    if isinstance(error, _SnapshotChanged):
        return _result(
            "damaged", selected, message="归档文件持续变化，暂时无法安全检查"
        )
    return _result("damaged", selected, message="无法读取所选归档目录")


def _root_is_unchanged(
    selected: Path,
    root_fd: int,
    root_identity: tuple[int, int, int, int],
) -> bool:
    try:
        selected_stat = os.lstat(selected)
        return (
            not stat.S_ISLNK(selected_stat.st_mode)
            and _identity(selected_stat) == root_identity
            and _identity(os.fstat(root_fd)) == root_identity
        )
    except OSError:
        return False


def _inspect_archive_attempt(
    selected: Path,
    current_uid: str | None,
) -> ArchiveFolderInspection:
    try:
        selected_stat = os.lstat(selected)
    except FileNotFoundError:
        return _result("empty", selected)
    if stat.S_ISLNK(selected_stat.st_mode):
        return _result("damaged", selected, message="所选归档根目录不能是符号链接")
    if not stat.S_ISDIR(selected_stat.st_mode):
        return _result(
            "ordinary_nonempty",
            selected,
            message="所选路径不是文件夹，请重新选择文件夹",
        )

    root_fd: int | None = None
    data_fd: int | None = None
    sources: tuple[_OpenedSource, ...] = ()
    try:
        try:
            root_fd = os.open(selected, _OPEN_DIRECTORY_FLAGS)
        except FileNotFoundError as exc:
            raise _SnapshotChanged("归档根目录已变化") from exc
        root_identity = _identity(selected_stat)
        if _identity(os.fstat(root_fd)) != root_identity:
            raise _SnapshotChanged("归档根目录已变化")
        entries = os.listdir(root_fd)
        if not entries:
            if not _root_is_unchanged(selected, root_fd, root_identity):
                raise _SnapshotChanged("归档根目录已变化")
            return _result("empty", selected)

        manifest_stat = _lstat_at(root_fd, MANIFEST_NAME)
        data_stat = _lstat_at(root_fd, "data")
        if manifest_stat is None and data_stat is None:
            legacy_stat = _lstat_at(root_fd, INDEX_FILENAME)
            if legacy_stat is not None:
                if stat.S_ISLNK(legacy_stat.st_mode):
                    raise _ArchiveSymlinkError(INDEX_FILENAME)
                if not stat.S_ISREG(legacy_stat.st_mode):
                    raise _ArchiveMarkerTypeError(INDEX_FILENAME)
                if not _root_is_unchanged(selected, root_fd, root_identity):
                    raise _SnapshotChanged("归档根目录已变化")
                return _result(
                    "legacy_index",
                    selected,
                    message="旧版备份目录，需要首次建立完整档案",
                )
            if not _root_is_unchanged(selected, root_fd, root_identity):
                raise _SnapshotChanged("归档根目录已变化")
            return _result(
                "ordinary_nonempty",
                selected,
                message="所选目录是普通非空文件夹",
            )

        if manifest_stat is not None:
            if stat.S_ISLNK(manifest_stat.st_mode):
                raise _ArchiveSymlinkError(MANIFEST_NAME)
            if not stat.S_ISREG(manifest_stat.st_mode):
                raise _ArchiveMarkerTypeError(MANIFEST_NAME)
        if data_stat is not None:
            if stat.S_ISLNK(data_stat.st_mode):
                raise _ArchiveSymlinkError("data")
            if not stat.S_ISDIR(data_stat.st_mode):
                raise _ArchiveMarkerTypeError("data")
        if manifest_stat is None or data_stat is None:
            if not _root_is_unchanged(selected, root_fd, root_identity):
                raise _SnapshotChanged("归档根目录已变化")
            return _result(
                "damaged",
                selected,
                message="归档清单与归档数据库不完整",
            )

        data_fd = _require_directory_at(root_fd, "data")
        data_identity = _identity(os.fstat(data_fd))
        database_stat = _lstat_at(data_fd, DATABASE_NAME)
        if database_stat is None:
            if not _snapshot_source_unchanged(
                (),
                root_fd,
                data_fd,
                data_identity,
                wal_was_absent=_lstat_at(data_fd, f"{DATABASE_NAME}-wal") is None,
                selected=selected,
                root_identity=root_identity,
            ):
                raise _SnapshotChanged("归档数据目录已变化")
            return _result(
                "damaged", selected, message="归档清单与归档数据库不完整"
            )
        if stat.S_ISLNK(database_stat.st_mode):
            raise _ArchiveSymlinkError(DATABASE_NAME)
        if not stat.S_ISREG(database_stat.st_mode):
            raise _ArchiveMarkerTypeError(DATABASE_NAME)

        with tempfile.TemporaryDirectory(prefix="weishushu-inspect-") as temporary:
            manifest_bytes, snapshot_database, sources, wal_was_absent = (
                _copy_snapshot_once(root_fd, data_fd, Path(temporary))
            )

            pending_result: ArchiveFolderInspection | None = None
            manifest: dict | None = None
            total_posts = 0
            try:
                manifest = _read_manifest(manifest_bytes)
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                pending_result = _result(
                    "damaged", selected, message="归档清单损坏，无法读取"
                )

            if pending_result is None:
                connection: sqlite3.Connection | None = None
                try:
                    connection, total_posts = _validate_database(snapshot_database)
                except _IntegrityCheckFailed:
                    pending_result = _result(
                        "damaged",
                        selected,
                        message="归档数据库完整性检查未通过",
                    )
                except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
                    pending_result = _result(
                        "damaged", selected, message="归档数据库损坏，无法打开"
                    )
                finally:
                    if connection is not None:
                        connection.close()

            if not _snapshot_source_unchanged(
                sources,
                root_fd,
                data_fd,
                data_identity,
                wal_was_absent,
                selected=selected,
                root_identity=root_identity,
            ):
                raise _SnapshotChanged("归档文件已变化")
            if pending_result is not None:
                return pending_result

            assert manifest is not None
            identity = {
                "uid": manifest["uid"],
                "screen_name": manifest["screen_name"],
                "total_posts": total_posts,
                "last_successful_sync_at": manifest["last_successful_sync_at"],
            }
            if current_uid is not None and manifest["uid"] != current_uid:
                return _result(
                    "uid_mismatch",
                    selected,
                    **identity,
                    message="该微博书属于其他账号，请登录归档所属账号",
                )
            return _result("archive", selected, **identity)
    finally:
        _close_sources(sources)
        if data_fd is not None:
            os.close(data_fd)
        if root_fd is not None:
            os.close(root_fd)


def _inspect_archive_path_attempt(
    selected: Path,
    current_uid: str | None,
) -> ArchiveFolderInspection:
    """在不支持目录文件描述符的平台读取稳定的归档快照。"""
    try:
        selected_stat = selected.lstat()
    except FileNotFoundError:
        return _result("empty", selected)
    if stat.S_ISLNK(selected_stat.st_mode):
        return _result("damaged", selected, message="所选归档根目录不能是符号链接")
    if not stat.S_ISDIR(selected_stat.st_mode):
        return _result(
            "ordinary_nonempty",
            selected,
            message="所选路径不是文件夹，请重新选择文件夹",
        )

    root_identity = _identity(selected_stat)
    data_path = selected / "data"
    try:
        entries = tuple(selected.iterdir())
    except FileNotFoundError as exc:
        raise _SnapshotChanged("归档根目录已变化") from exc
    if not entries:
        if not _path_root_is_unchanged(selected, root_identity):
            raise _SnapshotChanged("归档根目录已变化")
        return _result("empty", selected)

    manifest_path = selected / MANIFEST_NAME
    manifest_stat = _lstat_path(manifest_path)
    data_stat = _lstat_path(data_path)
    if manifest_stat is None and data_stat is None:
        legacy_path = selected / INDEX_FILENAME
        legacy_stat = _lstat_path(legacy_path)
        if legacy_stat is not None:
            if stat.S_ISLNK(legacy_stat.st_mode):
                raise _ArchiveSymlinkError(INDEX_FILENAME)
            if not stat.S_ISREG(legacy_stat.st_mode):
                raise _ArchiveMarkerTypeError(INDEX_FILENAME)
            if not _path_root_is_unchanged(selected, root_identity):
                raise _SnapshotChanged("归档根目录已变化")
            return _result(
                "legacy_index",
                selected,
                message="旧版备份目录，需要首次建立完整档案",
            )
        if not _path_root_is_unchanged(selected, root_identity):
            raise _SnapshotChanged("归档根目录已变化")
        return _result(
            "ordinary_nonempty",
            selected,
            message="所选目录是普通非空文件夹",
        )

    if manifest_stat is not None:
        if stat.S_ISLNK(manifest_stat.st_mode):
            raise _ArchiveSymlinkError(MANIFEST_NAME)
        if not stat.S_ISREG(manifest_stat.st_mode):
            raise _ArchiveMarkerTypeError(MANIFEST_NAME)
    if data_stat is not None:
        if stat.S_ISLNK(data_stat.st_mode):
            raise _ArchiveSymlinkError("data")
        if not stat.S_ISDIR(data_stat.st_mode):
            raise _ArchiveMarkerTypeError("data")
    if manifest_stat is None or data_stat is None:
        if not _path_root_is_unchanged(selected, root_identity):
            raise _SnapshotChanged("归档根目录已变化")
        return _result(
            "damaged",
            selected,
            message="归档清单与归档数据库不完整",
        )

    data_identity = _identity(data_stat)
    database_path = data_path / DATABASE_NAME
    database_stat = _lstat_path(database_path)
    wal_path = data_path / f"{DATABASE_NAME}-wal"
    if database_stat is None:
        if not _path_snapshot_unchanged(
            (),
            selected,
            root_identity,
            data_path,
            data_identity,
            wal_path,
            wal_was_absent=_lstat_path(wal_path) is None,
        ):
            raise _SnapshotChanged("归档数据目录已变化")
        return _result("damaged", selected, message="归档清单与归档数据库不完整")
    if stat.S_ISLNK(database_stat.st_mode):
        raise _ArchiveSymlinkError(DATABASE_NAME)
    if not stat.S_ISREG(database_stat.st_mode):
        raise _ArchiveMarkerTypeError(DATABASE_NAME)

    with tempfile.TemporaryDirectory(prefix="weishushu-inspect-") as temporary:
        temporary_root = Path(temporary)
        manifest_bytes, manifest_source = _read_path_source(manifest_path)
        database_source = _copy_path_source(
            database_path,
            temporary_root / DATABASE_NAME,
        )
        sources: list[_CopiedPathSource] = [manifest_source, database_source]
        wal_was_absent = _lstat_path(wal_path) is None
        if not wal_was_absent:
            sources.append(
                _copy_path_source(wal_path, temporary_root / f"{DATABASE_NAME}-wal")
            )

        pending_result: ArchiveFolderInspection | None = None
        manifest: dict | None = None
        total_posts = 0
        try:
            manifest = _read_manifest(manifest_bytes)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            pending_result = _result(
                "damaged", selected, message="归档清单损坏，无法读取"
            )

        if pending_result is None:
            connection: sqlite3.Connection | None = None
            try:
                connection, total_posts = _validate_database(
                    temporary_root / DATABASE_NAME
                )
            except _IntegrityCheckFailed:
                pending_result = _result(
                    "damaged",
                    selected,
                    message="归档数据库完整性检查未通过",
                )
            except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
                pending_result = _result(
                    "damaged", selected, message="归档数据库损坏，无法打开"
                )
            finally:
                if connection is not None:
                    connection.close()

        if not _path_snapshot_unchanged(
            tuple(sources),
            selected,
            root_identity,
            data_path,
            data_identity,
            wal_path,
            wal_was_absent,
        ):
            raise _SnapshotChanged("归档文件已变化")
        if pending_result is not None:
            return pending_result

        assert manifest is not None
        identity = {
            "uid": manifest["uid"],
            "screen_name": manifest["screen_name"],
            "total_posts": total_posts,
            "last_successful_sync_at": manifest["last_successful_sync_at"],
        }
        if current_uid is not None and manifest["uid"] != current_uid:
            return _result(
                "uid_mismatch",
                selected,
                **identity,
                message="该微博书属于其他账号，请登录归档所属账号",
            )
        return _result("archive", selected, **identity)


def inspect_archive_folder(
    path: str | Path,
    current_uid: str | None,
) -> ArchiveFolderInspection:
    """识别所选目录，不创建、修复或更改其中任何内容。"""
    selected = Path(path)
    inspect_attempt = (
        _inspect_archive_attempt
        if _SUPPORTS_DIRECTORY_FDS
        else _inspect_archive_path_attempt
    )
    for _ in range(_SNAPSHOT_ATTEMPTS):
        try:
            return inspect_attempt(selected, current_uid)
        except _SnapshotChanged:
            continue
        except (_ArchiveSymlinkError, _ArchiveMarkerTypeError) as exc:
            return _damaged_for_marker_error(selected, exc)
        except OSError:
            return _result("damaged", selected, message="无法读取所选归档目录")
    return _damaged_for_marker_error(selected, _SnapshotChanged("归档文件持续变化"))


_ILLEGAL_FOLDER_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE_RUNS = re.compile(r"\s+")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_FOLDER_NAME_MAX = 60


def archive_folder_name(screen_name: str, uid: str) -> str:
    """微博书子文件夹名：``{昵称}_{UID}``。

    过滤 Windows/macOS 都不合法的字符，避开 Windows 保留名，限制昵称段
    长度；昵称为空时退回「微博书」。UID 后缀保证唯一与可识别。
    """
    cleaned = _ILLEGAL_FOLDER_NAME_CHARS.sub("_", screen_name)
    cleaned = _WHITESPACE_RUNS.sub(" ", cleaned).strip().strip(".").strip()
    if not cleaned:
        cleaned = "微博书"
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_"
    cleaned = cleaned[:_FOLDER_NAME_MAX].rstrip(" .")
    if not cleaned:
        cleaned = "微博书"
    return f"{cleaned}_{uid}"


def find_child_archives(
    selected: str | Path,
    uid: str,
    *,
    inspector: Callable[..., ArchiveFolderInspection] = inspect_archive_folder,
) -> list[ArchiveFolderInspection]:
    """在所选目录的直接子文件夹里按清单 UID 找本账号的已有微博书（只读）。

    不按文件夹名匹配（用户可能改名），只看清单里的 UID；跳过隐藏目录和
    没有归档清单的目录。
    """
    selected_path = Path(selected)
    try:
        children = sorted(
            (
                child
                for child in selected_path.iterdir()
                if child.is_dir() and not child.is_symlink()
            ),
            key=lambda child: child.name,
        )
    except OSError:
        return []
    found: list[ArchiveFolderInspection] = []
    for child in children:
        if child.name.startswith(".") or not (child / MANIFEST_NAME).is_file():
            continue
        inspection = inspector(child, current_uid=uid)
        if inspection.state == "archive":
            found.append(inspection)
    return found


def resolve_archive_dir(
    selected: str | Path,
    uid: str,
    screen_name: str,
    *,
    inspector: Callable[..., ArchiveFolderInspection] = inspect_archive_folder,
) -> str:
    """把用户所选目录解析为实际微博书根目录（只读，不创建任何内容）。

    所选目录本身就是微博书/旧版备份/他人档案/损坏目录时原样返回，模式
    合法性仍由调用方的模式检查把关；否则在直接子文件夹里按清单 UID 寻
    找本账号的已有微博书，找到唯一一个就续用；都没有时以 ``{昵称}_{UID}``
    子文件夹作为新建目标，避免档案文件和用户其它文件混在同一层。
    """
    selected_path = Path(selected)
    inspection = inspector(selected, current_uid=uid)
    if inspection.state in {"archive", "legacy_index", "uid_mismatch", "damaged"}:
        return str(selected_path)
    folder_name = archive_folder_name(screen_name, uid)
    if selected_path.name == folder_name:
        # 幂等：解析结果再次传入时直接返回，避免嵌套出 昵称_UID/昵称_UID。
        return str(selected_path)
    found = find_child_archives(selected_path, uid, inspector=inspector)
    if len(found) > 1:
        raise WeiboError(
            "所选文件夹下有多个当前账号的微博书，请直接选择其中一个",
            kind=WeiboErrorKind.API,
        )
    if found:
        return found[0].path
    target = selected_path / folder_name
    if target.exists() or target.is_symlink():
        target_inspection = inspector(target, current_uid=uid)
        if target_inspection.state == "uid_mismatch":
            raise WeiboError("该微博书属于其他账号，不允许覆盖", kind=WeiboErrorKind.AUTH)
        if target_inspection.state == "damaged":
            raise WeiboError("所选微博书已损坏，请先处理目录问题", kind=WeiboErrorKind.PARSE)
        if target_inspection.state != "empty":
            raise WeiboError(
                f"子文件夹「{target.name}」已存在且不是微博书，请手动处理或改选其它文件夹",
                kind=WeiboErrorKind.API,
            )
    return str(target)


def inspect_selected_folder(
    selected: str | Path,
    uid: str,
    *,
    inspector: Callable[..., ArchiveFolderInspection] = inspect_archive_folder,
) -> ArchiveFolderInspection:
    """检查用户所选目录（只读）。

    普通非空目录下若唯一存在本账号微博书子文件夹，返回该子文件夹的检查
    结果，让用户再次选择同一个父目录时直接续上已有微博书。
    """
    inspection = inspector(selected, current_uid=uid)
    if inspection.state != "ordinary_nonempty":
        return inspection
    found = find_child_archives(selected, uid, inspector=inspector)
    if len(found) != 1:
        return inspection
    return found[0]
