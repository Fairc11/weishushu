"""v1.2.0 M3-5: frozen/dev 双路径 smoke test。

覆盖：
- desktop_app frozen import 不崩（M3-1 修过的 datetime）
    - frozen 模式日志目录走平台用户日志目录
- WebView2 检测不抛异常
- settings.output_dir frozen 下不写安装目录

注意：M3-5 不做 importlib.reload（会污染 root logger 状态，影响其他测试）。
- frozen import 改成静态源码检查（验证 import 存在）
- frozen log dir 改成 save/restore logging 状态 + 直接调 _setup_logging()
"""
import logging
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
    """上下文管理器：临时设置 sys.frozen=True 测 frozen 路径，并写入构建清单。"""

    def __init__(self, value: bool = True):
        self.value = value
        self._orig = getattr(sys, "frozen", None)
        self._had = hasattr(sys, "frozen")
        self._orig_executable = getattr(sys, "executable", None)
        self._had_executable = hasattr(sys, "executable")
        self._tmp: tempfile.TemporaryDirectory | None = None

    def _ensure_tmp(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

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
                app_root = _P(self._ensure_tmp()) / f"{name}.app" / "Contents" / "MacOS"
                manifest_dir = (
                    _P(self._ensure_tmp()) / f"{name}.app" / "Contents" / "Resources"
                )
            _write_frozen_manifest(manifest_dir, platform=sys.platform, name=name)
            sys.executable = str(app_root / name)
        return self

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


def _save_root_logging():
    """保存 root logger 状态。"""
    root = logging.getLogger()
    return list(root.handlers), root.level


def _restore_root_logging(saved):
    """恢复 root logger 状态 + 关所有 handler。"""
    orig_handlers, orig_level = saved
    logging.shutdown()
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass
    for h in orig_handlers:
        root.addHandler(h)
    root.setLevel(orig_level)


# ====== 1. frozen 模式 import desktop_app 不崩（静态源码检查） ======

class FrozenImportSmokeTests(unittest.TestCase):
    """M3-5.1: 静态检查 desktop_app.py 顶部 import 了 datetime。

    之前 v1.1.6 desktop_app.py:29 用了 datetime.now() 但没 import → frozen 启动 NameError。
    M3-1 修复：加 from datetime import datetime。
    """

    def test_desktop_app_source_imports_datetime(self):
        """源码层：datetime 已 import。"""
        desktop_app_path = Path(__file__).resolve().parents[1] / "desktop_app.py"
        src = desktop_app_path.read_text(encoding="utf-8")
        # 关键：源码必须同时满足 ① 用了 datetime.now() ② import 了 datetime
        self.assertIn("datetime.now()", src, "desktop_app.py 用了 datetime.now()")
        self.assertIn(
            "from datetime import datetime",
            src,
            "M3-1 修复：缺 import 会 NameError on frozen startup",
        )


# ====== 2. frozen 模式日志目录走平台日志目录 ======

class FrozenLogDirSmokeTests(unittest.TestCase):
    """M3-5.2: frozen 模式下 _setup_logging 在平台日志目录写盘。

    B07 v1.2.0: 日志目录统一为英文 logs/（不再用中文"日志/"）。
    路径来源：backend.app.config.settings.log_dir（不再硬编码）。
    """

    def test_frozen_log_dir_uses_platform_log_dir(self):
        from desktop_app import _setup_logging

        with tempfile.TemporaryDirectory() as fake_home, \
             patch.dict(os.environ, {"HOME": fake_home}, clear=False), \
             patch.object(sys, "platform", "darwin"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(fake_home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(fake_home)), \
             _FrozenSwitch(True):
            saved = _save_root_logging()
            try:
                _setup_logging()  # frozen=True 时会建平台日志目录
                expected = Path(fake_home) / "Library" / "Logs" / "Weishushu"
                self.assertTrue(
                    expected.exists(),
                    f"frozen 模式日志目录未创建: {expected}",
                )
            finally:
                _restore_root_logging(saved)


# ====== 3. WebView2 检测不抛异常 ======

class WebView2CheckSmokeTests(unittest.TestCase):
    """M3-5.3: WebView2 检测函数能跑完，不抛异常。"""

    def test_webview2_check_does_not_raise(self):
        from backend.app.services.setup_check import check_webview2_installed
        try:
            result = check_webview2_installed()
        except Exception as e:
            self.fail(f"WebView2 检测抛异常: {type(e).__name__}: {e}")
        self.assertIsInstance(result, bool)


# ====== 4. settings.output_dir frozen 下不写安装目录 ======

class SettingsOutputDirSmokeTests(unittest.TestCase):
    """M3-5.4: settings.output_dir frozen 走平台用户数据路径，
    不写 C:\\Program Files\\Weishushu\\（无写权限会爆）。"""

    def test_frozen_output_dir_avoids_program_files(self):
        from backend.app.config import Settings

        with tempfile.TemporaryDirectory() as fake_home, \
             patch.object(sys, "platform", "darwin"), \
             patch("backend.app.platform_paths.Path.home", return_value=Path(fake_home)), \
             patch("backend.app.runtime_context.Path.home", return_value=Path(fake_home)), \
             _FrozenSwitch(True):
            s = Settings()
            out_str = str(s.output_dir).replace("/", "\\")
            self.assertNotIn("Program Files", out_str, f"frozen 写安装目录: {out_str}")
            self.assertIn("Weishushu", out_str, f"frozen 应走 Weishushu 目录: {out_str}")

    def test_dev_output_dir_uses_cwd_output(self):
        """dev 模式（sys.frozen 缺省）走 ./output。"""
        from backend.app.config import Settings
        if hasattr(sys, "frozen"):
            del sys.frozen
        s = Settings()
        out_str = str(s.output_dir)
        self.assertTrue(
            out_str.replace("\\", "/").endswith("/output") or
            out_str.replace("\\", "/").endswith("/output/"),
            f"dev output_dir 应在 ./output: {out_str}",
        )

    def test_settings_version_is_v2_0_1(self):
        """当前主线固定：config.Settings().version 应是 v2.0.1。"""
        from backend.app.config import Settings
        s = Settings()
        self.assertEqual(s.version, "2.0.1")


if __name__ == "__main__":
    unittest.main()
