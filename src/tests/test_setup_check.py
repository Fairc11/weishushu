"""v1.1.3 D1+D3 setup_check 单测。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.services.setup_check import (
    find_chromium_executable,
    check_chromium_ready,
    PLAYWRIGHT_CHROMIUM_NAMES,
    get_localappdata_ms_playwright,
)


class ChromiumDiscoveryTests(unittest.TestCase):
    """D1: Playwright Chromium 5 文件名兼容"""

    def test_5_filenames_constant(self):
        """5 种命名（抖音 v1.4.2 经验）全列"""
        expected = {
            "chrome-headless-shell.exe", "headless_shell.exe",
            "chromium-headless-shell.exe", "chrome.exe", "chromium.exe",
        }
        self.assertEqual(set(PLAYWRIGHT_CHROMIUM_NAMES), expected)

    def test_finds_chrome_headless_shell_first(self):
        """chrome-headless-shell.exe 是当前官方命名（应最先匹配）"""
        self.assertEqual(PLAYWRIGHT_CHROMIUM_NAMES[0], "chrome-headless-shell.exe")

    def test_returns_none_when_no_chromium(self):
        """没安装时返回 None（不抛异常）"""
        # 假设测试环境没有内置 chromium
        with patch("backend.app.services.setup_check.get_localappdata_ms_playwright") as mock_l, \
             patch("backend.app.services.setup_check.get_frozen_ms_playwright") as mock_f:
            mock_l.return_value = Path("/nonexistent/path/that/does/not/exist")
            mock_f.return_value = None
            result = find_chromium_executable()
            # 找不着时返回 None
            self.assertIsNone(result)

    def test_check_returns_structured(self):
        """check_chromium_ready 返回 dict 含 5 字段"""
        result = check_chromium_ready()
        self.assertIn("chromium_executable", result)
        self.assertIn("chromium_ready", result)
        self.assertIn("webview2_installed", result)
        self.assertIn("platform", result)
        self.assertIn("frozen", result)
        self.assertIsInstance(result["chromium_ready"], bool)

    def test_localappdata_path(self):
        """get_localappdata_ms_playwright 返回正确目录"""
        path = get_localappdata_ms_playwright()
        if sys.platform == "win32":
            self.assertIn("ms-playwright", str(path))
        else:
            self.assertIn("ms-playwright", str(path))

    def test_macos_uses_library_caches_playwright_directory(self):
        with patch("backend.app.services.setup_check.sys.platform", "darwin"):
            path = get_localappdata_ms_playwright()
        self.assertEqual(path, Path.home() / "Library" / "Caches" / "ms-playwright")

    def test_finds_chromium_in_configured_browsers_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            browser = Path(td) / "chromium-test" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            browser.parent.mkdir(parents=True)
            browser.write_text("browser", encoding="utf-8")
            with patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": td}, clear=False), \
                 patch("backend.app.services.setup_check.sys.platform", "darwin"), \
                 patch("backend.app.services.setup_check.get_frozen_ms_playwright", return_value=None):
                self.assertEqual(find_chromium_executable(), browser)

    def test_windows_frozen_accepts_direct_internal_chromium_layout(self):
        import tempfile

        from backend.app.services.setup_check import get_frozen_ms_playwright
        from packaging.build_manifest import make_manifest, write_manifest

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            internal = root / "_internal"
            direct = internal / "chromium_headless_shell-test"
            direct.mkdir(parents=True)
            write_manifest(
                internal / "weishushu_build_manifest.json",
                make_manifest(
                    app_version="2.0.0",
                    source_commit="frozen-test-commit",
                    platform="win32",
                    architecture="x64",
                    python_version="3.12.13",
                    pyinstaller_version="6.0.0",
                    dependency_lock_sha256="d" * 64,
                    profile="user",
                    executable_name="Weishushu.exe",
                    bundle_identifier="com.weishushu.desktop",
                    resources=[],
                ),
            )
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "platform", "win32"), \
                 patch.object(sys, "executable", str(root / "Weishushu.exe")):
                self.assertEqual(get_frozen_ms_playwright(), internal)

    def test_windows_build_spec_bundles_chromium_under_ms_playwright(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "build.spec").read_text(encoding="utf-8")
        self.assertIn('f"ms-playwright/{leaf}"', spec)


class WebView2Tests(unittest.TestCase):
    """D3: WebView2 64/32/WOW6432Node 三处注册表检测"""

    @unittest.skipIf(sys.platform != "win32", "Windows only")
    def test_webview2_check_runs(self):
        """WebView2 检测在 Windows 上能跑（结果不强制——开发机可能没装）"""
        from backend.app.services.setup_check import check_webview2_installed
        result = check_webview2_installed()
        self.assertIsInstance(result, bool)

    def test_webview2_check_non_windows_returns_true(self):
        """非 Windows 平台跳过 WebView2 检测（pywebview 走系统 WebKit）"""
        from backend.app.services.setup_check import check_webview2_installed
        with patch("sys.platform", "linux"):
            result = check_webview2_installed()
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
