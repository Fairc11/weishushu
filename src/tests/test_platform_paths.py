"""平台路径策略测试：Mac 主线 + Windows 历史构建。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_frozen_manifest(directory: Path, *, platform: str, name: str) -> None:
    from packaging.build_manifest import make_manifest, write_manifest

    profile = "user"
    bundle_id = "com.weishushu.desktop"
    if name == "WeishushuDev":
        profile = "dev"
        bundle_id = "com.weishushu.desktop.dev"
    write_manifest(
        directory / "weishushu_build_manifest.json",
        make_manifest(
            app_version="2.0.0",
            source_commit="frozen-test-commit",
            platform=platform,
            architecture="arm64",
            python_version="3.12.13",
            pyinstaller_version="6.0.0",
            dependency_lock_sha256="d" * 64,
            profile=profile,
            executable_name=name,
            bundle_identifier=bundle_id,
            resources=[],
        ),
    )


class _FrozenSwitch:
    """进入 frozen 模拟时建立临时 .app/onedir 布局并写入构建清单。"""

    def __init__(self, value: bool = True):
        self.value = value
        self._orig = getattr(sys, "frozen", None)
        self._had = hasattr(sys, "frozen")
        self._had_executable = hasattr(sys, "executable")
        self._orig_executable = getattr(sys, "executable", None)
        self._tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self):
        sys.frozen = self.value
        if self.value:
            from pathlib import Path as _P
            name = _P(sys.executable).name
            if sys.platform == "win32":
                if name != "Weishushu.exe":
                    name = "Weishushu.exe"
                app_root = _P(self._ensure_tmp()) / "Weishushu"
                manifest_dir = app_root / "_internal"
            else:
                if name not in {"Weishushu", "WeishushuDev"}:
                    name = "Weishushu"
                app_root = _P(self._ensure_tmp()) / f"{name}.app" / "Contents"
                manifest_dir = app_root / "Resources"
                app_root = app_root / "MacOS"
            _write_frozen_manifest(manifest_dir, platform=sys.platform, name=name)
            sys.executable = str(app_root / name)
        return self

    def _ensure_tmp(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *args):
        if self._had:
            sys.frozen = self._orig
        elif hasattr(sys, "frozen"):
            del sys.frozen
        if self._had_executable:
            sys.executable = self._orig_executable
        elif hasattr(sys, "executable"):
            del sys.executable
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


class PlatformPathTests(unittest.TestCase):
    def test_dev_paths_stay_inside_project_runtime_dirs(self):
        from backend.app.platform_paths import PlatformPaths

        with tempfile.TemporaryDirectory() as td, patch.object(sys, "platform", "darwin"):
            cwd = Path(td)
            paths = PlatformPaths(cwd=cwd)

            self.assertEqual(paths.output_dir(), cwd / "output")
            self.assertEqual(paths.local_app_data_dir(), cwd / ".run")
            self.assertEqual(paths.state_dir(), cwd / ".run" / "state")
            self.assertEqual(paths.log_dir(), cwd / ".run" / "logs")
            self.assertEqual(paths.cache_dir(), cwd / ".run" / "cache")
            self.assertEqual(paths.browser_cookie_file(), cwd / ".run" / "state" / "cookies.json")
            self.assertEqual(
                paths.persistent_task_file(),
                cwd / ".run" / "state" / "active-personal-archive-task.json",
            )

    def test_frozen_macos_paths_use_library_dirs(self):
        from backend.app.platform_paths import PlatformPaths

        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home}, clear=False), \
             patch.object(sys, "platform", "darwin"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(home)), \
             _FrozenSwitch(True):
            paths = PlatformPaths()
            root = Path(home)

            self.assertEqual(
                paths.local_app_data_dir(),
                root / "Library" / "Application Support" / "Weishushu",
            )
            self.assertEqual(paths.output_dir(), paths.local_app_data_dir() / "output")
            self.assertEqual(paths.state_dir(), paths.local_app_data_dir() / "state")
            self.assertEqual(paths.log_dir(), root / "Library" / "Logs" / "Weishushu")
            self.assertEqual(paths.cache_dir(), root / "Library" / "Caches" / "Weishushu")
            self.assertEqual(paths.browser_cookie_file(), paths.state_dir() / "cookies.json")
            self.assertEqual(
                paths.persistent_task_file(),
                paths.state_dir() / "active-personal-archive-task.json",
            )

    def test_frozen_windows_paths_keep_localappdata_history(self):
        from backend.app.platform_paths import PlatformPaths

        with tempfile.TemporaryDirectory() as localappdata, \
             patch.dict(os.environ, {"LOCALAPPDATA": localappdata}, clear=False), \
             patch.object(sys, "platform", "win32"), \
             _FrozenSwitch(True):
            paths = PlatformPaths()
            base = Path(localappdata) / "Weishushu"

            self.assertEqual(paths.local_app_data_dir(), base)
            self.assertEqual(paths.output_dir(), base / "output")
            self.assertEqual(paths.state_dir(), base / "state")
            self.assertEqual(paths.log_dir(), base / "logs")
            self.assertEqual(paths.cache_dir(), base / "cache")

    def test_cookie_candidates_start_with_primary_weibo_book_cookie_file(self):
        from backend.app.platform_paths import PlatformPaths
        from weibo_book.login import get_cookie_file_path

        with tempfile.TemporaryDirectory() as td:
            paths = PlatformPaths(cwd=Path(td))
            candidates = paths.cookie_file_candidates()

            self.assertEqual(candidates[0], get_cookie_file_path())
            self.assertIn(paths.browser_cookie_file(), candidates)

    def test_settings_use_platform_paths(self):
        from backend.app.config import Settings

        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home}, clear=False), \
             patch.object(sys, "platform", "darwin"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(home)), \
             _FrozenSwitch(True):
            settings = Settings()

            self.assertEqual(
                settings.local_app_data_dir,
                Path(home) / "Library" / "Application Support" / "Weishushu",
            )
            self.assertEqual(settings.output_dir, settings.local_app_data_dir / "output")
            self.assertEqual(settings.log_dir, Path(home) / "Library" / "Logs" / "Weishushu")
            self.assertEqual(settings.state_dir, settings.local_app_data_dir / "state")

    def test_first_run_marker_uses_state_dir(self):
        from backend.app.services.first_run import MARKER_FILENAME, _marker_path

        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home}, clear=False), \
             patch.object(sys, "platform", "darwin"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(home)), \
             _FrozenSwitch(True):
            marker = _marker_path()

            self.assertEqual(
                marker,
                Path(home) / "Library" / "Application Support" / "Weishushu" / "state" / MARKER_FILENAME,
            )

    def test_dev_profile_uses_isolated_dirname_on_macos(self):
        """v2.0.0 阶段 3：开发版 profile 切数据目录到 WeishushuDev，避免污染用户版。"""
        from backend.app.platform_paths import PlatformPaths

        executable = "/Applications/WeishushuDev.app/Contents/MacOS/WeishushuDev"
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home, "WEISHUSHU_PROFILE": "dev"}, clear=False), \
             patch.object(sys, "platform", "darwin"), \
             patch.object(sys, "executable", executable), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(home)), \
             _FrozenSwitch(True):
            paths = PlatformPaths()
            root = Path(home)

            self.assertEqual(
                paths.local_app_data_dir(),
                root / "Library" / "Application Support" / "WeishushuDev",
            )
            self.assertEqual(paths.log_dir(), root / "Library" / "Logs" / "WeishushuDev")
            self.assertEqual(paths.cache_dir(), root / "Library" / "Caches" / "WeishushuDev")
            self.assertEqual(paths.state_dir(), paths.local_app_data_dir() / "state")

    def test_finder_launched_dev_bundle_uses_isolated_macos_paths_without_environment(self):
        """开发版 `.app` 从 Finder 启动时仍须使用独立数据、日志和缓存目录。"""
        from backend.app.platform_paths import PlatformPaths

        executable = "/Applications/WeishushuDev.app/Contents/MacOS/WeishushuDev"
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home}, clear=True), \
             patch.object(sys, "platform", "darwin"), \
             patch.object(sys, "executable", executable), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(home)), \
             _FrozenSwitch(True):
            paths = PlatformPaths()
            root = Path(home)

            self.assertEqual(
                paths.local_app_data_dir(),
                root / "Library" / "Application Support" / "WeishushuDev",
            )
            self.assertEqual(paths.log_dir(), root / "Library" / "Logs" / "WeishushuDev")
            self.assertEqual(paths.cache_dir(), root / "Library" / "Caches" / "WeishushuDev")

    def test_dev_profile_uses_isolated_dirname_on_windows(self):
        """Windows 源码开发态使用开发目录；正式 frozen 不接受环境切换。"""
        from backend.app.platform_paths import PlatformPaths

        with tempfile.TemporaryDirectory() as localappdata, \
             patch.dict(os.environ, {"LOCALAPPDATA": localappdata, "WEISHUSHU_PROFILE": "dev"}, clear=False), \
             patch.object(sys, "platform", "win32"), \
             _FrozenSwitch(False):
            paths = PlatformPaths()
            base = Path.cwd() / ".run"

            self.assertEqual(paths.local_app_data_dir(), base)
            self.assertEqual(paths.state_dir(), base / "state")
            self.assertEqual(paths.cache_dir(), base / "cache")
            self.assertEqual(paths.log_dir(), base / "logs")

    def test_dev_profile_cookie_file_isolated_from_user_profile(self):
        """v2.0.0 阶段 3：开发版 Cookie 文件独立，避免污染用户版登录态。"""
        from backend.app.platform_paths import PlatformPaths
        from weibo_book.login import get_cookie_file_path

        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home, "WEISHUSHU_PROFILE": "dev"}, clear=False), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(home)):
            paths = PlatformPaths(cwd=Path(home))
            candidates = paths.cookie_file_candidates()
            primary = candidates[0]

            self.assertEqual(primary, get_cookie_file_path())
            self.assertEqual(primary.name, ".weibo_book_cookies_dev")
            self.assertEqual(primary, Path(home) / ".weibo_book_cookies_dev")

    def test_dev_profile_excludes_formal_legacy_windows_cookie(self):
        """开发端候选链不得读取正式身份的旧 Windows Cookie。"""
        from backend.app.platform_paths import PlatformPaths

        with tempfile.TemporaryDirectory() as td, \
             patch.dict(
                 os.environ,
                 {
                     "HOME": td,
                     "LOCALAPPDATA": str(Path(td) / "Local"),
                     "WEISHUSHU_PROFILE": "dev",
                 },
                 clear=True,
             ), \
             patch.object(sys, "platform", "win32"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(td)), \
             _FrozenSwitch(False):
            paths = PlatformPaths(cwd=Path(td) / "source")

            self.assertNotIn(
                paths.legacy_windows_cookie_file(),
                paths.cookie_file_candidates(),
            )

    def test_unknown_frozen_executable_stops_path_resolution(self):
        from backend.app.platform_paths import PlatformPaths

        with patch.object(sys, "platform", "darwin"), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", "/Applications/Unknown/Unknown", create=True):
            from backend.app.runtime_context import RuntimeContextError
            with self.assertRaises(RuntimeContextError):
                PlatformPaths().local_app_data_dir()

    def test_profile_error_stops_default_cookie_resolution(self):
        from weibo_book.login import get_cookie_file_path

        error = ImportError("profile-cookie-failed")
        with patch(
            "backend.app.profile.default_cookie_filename",
            side_effect=error,
        ):
            with self.assertRaisesRegex(ImportError, "profile-cookie-failed"):
                get_cookie_file_path()

    def test_primary_cookie_error_is_not_replaced_with_user_cookie(self):
        from backend.app.platform_paths import PlatformPaths

        error = ImportError("primary-cookie-failed")
        with patch(
            "weibo_book.login.get_cookie_file_path",
            side_effect=error,
        ):
            with self.assertRaisesRegex(ImportError, "primary-cookie-failed"):
                PlatformPaths().primary_cookie_file()


if __name__ == "__main__":
    unittest.main()
