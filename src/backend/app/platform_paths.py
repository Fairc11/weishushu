"""平台路径集中策略。

dev 模式只写项目目录下的 `.run` / `output`；frozen 模式按平台写用户目录。
profile 通过 `WEISHUSHU_PROFILE=dev` 切换开发版数据目录名。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIRNAME = "Weishushu"  # 兼容历史引用；运行时通过 _app_dirname() 解析 profile
BROWSER_COOKIE_FILENAME = "cookies.json"
PERSISTENT_TASK_FILENAME = "active-personal-archive-task.json"
SELF_TEST_ROOT_ENV = "WEISHUSHU_SELF_TEST_ROOT"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _app_dirname() -> str:
    """从唯一 profile 事实源返回应用数据目录名；异常必须向上抛出。"""
    from backend.app.profile import app_dirname

    return app_dirname()


@dataclass(frozen=True)
class PlatformPaths:
    cwd: Path | None = None

    def _cwd(self) -> Path:
        return self.cwd or Path.cwd()

    def _use_context(self) -> bool:
        return is_frozen() or bool(os.environ.get(SELF_TEST_ROOT_ENV))

    def _context(self):
        from backend.app.runtime_context import resolve_runtime_context

        return resolve_runtime_context()

    def local_app_data_dir(self) -> Path:
        if self._use_context():
            return self._context().data_root
        if not is_frozen():
            return self._cwd() / ".run"
        dirname = _app_dirname()
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / dirname
        if sys.platform == "win32":
            localappdata = os.environ.get("LOCALAPPDATA")
            if not localappdata:
                localappdata = str(Path.home() / "AppData" / "Local")
            return Path(localappdata) / dirname
        xdg = os.environ.get("XDG_DATA_HOME")
        if not xdg:
            xdg = str(Path.home() / ".local" / "share")
        return Path(xdg) / dirname

    def log_dir(self) -> Path:
        if self._use_context():
            return self._context().log_root
        if not is_frozen():
            return self.local_app_data_dir() / "logs"
        dirname = _app_dirname()
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Logs" / dirname
        return self.local_app_data_dir() / "logs"

    def cache_dir(self) -> Path:
        if self._use_context():
            return self._context().cache_root
        if not is_frozen():
            return self.local_app_data_dir() / "cache"
        dirname = _app_dirname()
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Caches" / dirname
        if sys.platform == "win32":
            return self.local_app_data_dir() / "cache"
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if not xdg_cache:
            xdg_cache = str(Path.home() / ".cache")
        return Path(xdg_cache) / dirname

    def state_dir(self) -> Path:
        if self._use_context():
            return self._context().state_root
        return self.local_app_data_dir() / "state"

    def output_dir(self) -> Path:
        if self._use_context():
            return self._context().output_root
        if is_frozen():
            return self.local_app_data_dir() / "output"
        return self._cwd() / "output"

    def browser_cookie_file(self) -> Path:
        if self._use_context():
            return self._context().state_root / BROWSER_COOKIE_FILENAME
        return self.state_dir() / BROWSER_COOKIE_FILENAME

    def persistent_task_file(self) -> Path:
        if self._use_context():
            return self._context().state_root / PERSISTENT_TASK_FILENAME
        return self.state_dir() / PERSISTENT_TASK_FILENAME

    def legacy_windows_cookie_file(self) -> Path:
        localappdata = os.environ.get("LOCALAPPDATA")
        if not localappdata:
            localappdata = str(Path.home() / "AppData" / "Local")
        return Path(localappdata) / APP_DIRNAME / "日志" / BROWSER_COOKIE_FILENAME

    def primary_cookie_file(self) -> Path:
        if self._use_context():
            return self._context().cookie_file
        from weibo_book.login import get_cookie_file_path

        return get_cookie_file_path()

    def cookie_file_candidates(self) -> list[Path]:
        if self._use_context():
            return [self._context().cookie_file]
        from backend.app.profile import is_dev_profile

        candidates = [
            self.primary_cookie_file(),
            self.browser_cookie_file(),
        ]
        if sys.platform == "win32" and not is_dev_profile():
            candidates.append(self.legacy_windows_cookie_file())

        seen: set[Path] = set()
        result: list[Path] = []
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result


def platform_paths() -> PlatformPaths:
    return PlatformPaths()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_file_candidates() -> list[Path]:
    return platform_paths().cookie_file_candidates()
