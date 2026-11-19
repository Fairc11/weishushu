"""设置面板的系统通知与缓存维护；只做本机操作，不联网。"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from backend.app.platform_paths import platform_paths
from weibo_book.errors import WeiboError

logger = logging.getLogger(__name__)

KEEP_LOG_FILES = 5


def _cache_dir() -> Path:
    return platform_paths().cache_dir()


def _log_dir() -> Path:
    return platform_paths().log_dir()


def _dir_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob('*'):
        try:
            if path.is_file() or path.is_symlink():
                total += path.lstat().st_size
        except OSError:
            continue
    return total


def cache_items() -> list[dict]:
    update = _cache_dir() / 'update'
    logs = _log_dir()
    return [
        {'key': 'update', 'label': '更新包暂存', 'path': str(update), 'size_bytes': _dir_size(update)},
        {'key': 'logs', 'label': '运行日志', 'path': str(logs), 'size_bytes': _dir_size(logs)},
    ]


def clear_cache_item(key: str) -> dict:
    if key == 'update':
        root = _cache_dir() / 'update'
        freed = _dir_size(root)
        if root.is_dir():
            for child in root.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        return {'key': key, 'freed_bytes': freed}
    if key == 'logs':
        root = _log_dir()
        files = [p for p in root.rglob('*') if p.is_file()] if root.is_dir() else []
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        freed = 0
        for old in files[KEEP_LOG_FILES:]:
            try:
                freed += old.lstat().st_size
                old.unlink()
            except OSError:
                continue
        return {'key': key, 'freed_bytes': freed}
    raise WeiboError('未知的缓存项目，只能清理更新包暂存或运行日志')


def _applescript_escape(value: str) -> str:
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def send_notification(title: str, message: str, runner=None) -> dict:
    if sys.platform != 'darwin':
        return {'delivered': False, 'reason': '当前系统不支持系统通知'}

    def _run(argv: list[str]) -> None:
        subprocess.run(argv, check=True, capture_output=True, timeout=10)

    run = runner or _run
    script = (
        f'display notification "{_applescript_escape(message)}" '
        f'with title "{_applescript_escape(title)}"'
    )
    try:
        run(['osascript', '-e', script])
    except Exception as exc:
        logger.warning('系统通知发送失败：%s', exc)
        return {'delivered': False, 'reason': '系统通知发送失败'}
    return {'delivered': True}
