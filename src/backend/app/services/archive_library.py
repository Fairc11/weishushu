"""当前运行身份的本地书库登记；不复制档案数据、不联网。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from backend.app.config import settings
from backend.app.schemas import ArchiveLibraryEntry
from backend.app.services.archive_folder import (
    inspect_archive_folder,
    rename_legacy_self_archive,
)
from weibo_book.errors import WeiboError

_lock = RLock()


def _registry_path() -> Path:
    return settings.state_dir / 'archive-library.json'


def _read() -> list[dict[str, str]]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['version'] != 1 or not isinstance(data['entries'], list):
            raise ValueError('登记格式无效')
        for row in data['entries']:
            if set(row) != {'path', 'uid'} or not all(isinstance(v, str) and v for v in row.values()):
                raise ValueError('登记内容无效')
            if not Path(row['path']).is_absolute():
                raise ValueError('登记路径必须为绝对路径')
        return data['entries']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WeiboError('微博书库登记文件无法读取，请保留文件并检查') from exc


def _write(rows: list[dict[str, str]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.archive-library-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'version': 1, 'entries': rows}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise WeiboError('微博书库登记保存失败') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _inspect(path: str, expected_uid: str | None = None):
    inspection = inspect_archive_folder(path, current_uid=expected_uid)
    if inspection.state != 'archive' or not inspection.uid:
        raise WeiboError('所选目录不是有效微博书，或所属账号与登记不一致')
    return inspection


def register_archive(output_dir: str, expected_uid: str | None = None) -> None:
    output_dir = rename_legacy_self_archive(output_dir)
    inspection = _inspect(output_dir, expected_uid)
    row = {'path': inspection.path, 'uid': inspection.uid}
    with _lock:
        rows = _read()
        if any(item['path'] == row['path'] and item['uid'] != row['uid'] for item in rows):
            raise WeiboError('该路径已登记为其他账号，请先移除旧登记')
        if row not in rows:
            _write([*rows, row])


def list_archives() -> list[ArchiveLibraryEntry]:
    with _lock:
        rows = _read()
        # 旧版「我的微博」档案在列出时改名并同步登记路径。
        migrated = False
        for row in rows:
            renamed = rename_legacy_self_archive(row['path'])
            if renamed != row['path']:
                row['path'] = renamed
                migrated = True
        if migrated:
            _write(rows)
    entries = []
    for row in rows:
        inspection = inspect_archive_folder(row['path'], current_uid=row['uid'])
        available = inspection.state == 'archive'
        if available:
            format_version = inspection.book_format_version
        elif inspection.state == 'legacy_index':
            format_version = 1
        else:
            format_version = None
        entries.append(ArchiveLibraryEntry(
            **row, available=available,
            screen_name=inspection.screen_name if available else '',
            total_posts=inspection.total_posts if available else 0,
            last_successful_sync_at=inspection.last_successful_sync_at if available else '',
            history_complete=inspection.history_complete if available else True,
            history_cutoff=inspection.history_cutoff if available else '',
            book_format_version=format_version,
        ))
    return sorted(entries, key=lambda entry: entry.last_successful_sync_at, reverse=True)


_size_cache: dict[str, tuple[int, int]] = {}


def _archive_db_marker(path: Path) -> int:
    for candidate in (path / '数据与媒体' / '数据' / 'archive.db', path / 'data' / 'archive.db'):
        try:
            return candidate.stat().st_mtime_ns
        except OSError:
            continue
    return -1


def archive_size_bytes(path: str) -> int:
    root = Path(path)
    if not root.is_absolute():
        raise WeiboError('档案路径必须为绝对路径')
    if not root.is_dir():
        raise WeiboError('档案目录不存在或已移动')
    marker = _archive_db_marker(root)
    cached = _size_cache.get(str(root))
    if cached is not None and cached[1] == marker:
        return cached[0]
    total = 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                continue
    _size_cache[str(root)] = (total, marker)
    return total


def registered_archive_sizes() -> list[dict]:
    with _lock:
        rows = _read()
    sizes = []
    for row in rows:
        try:
            sizes.append({'path': row['path'], 'size_bytes': archive_size_bytes(row['path'])})
        except WeiboError:
            sizes.append({'path': row['path'], 'size_bytes': None})
    return sizes


def relocate_archive(path: str, uid: str, output_dir: str) -> None:
    inspection = _inspect(output_dir, uid)
    with _lock:
        rows = _read()
        old = {'path': path, 'uid': uid}
        if old not in rows:
            raise WeiboError('未找到原书库登记，请刷新后重试')
        new = {'path': inspection.path, 'uid': uid}
        if any(item['path'] == new['path'] and item['uid'] != uid for item in rows):
            raise WeiboError('新路径已登记为其他账号')
        rows.remove(old)
        if new not in rows:
            rows.append(new)
        _write(rows)


def remove_archive(path: str, uid: str) -> None:
    with _lock:
        rows = _read()
        _write([row for row in rows if row != {'path': path, 'uid': uid}])
