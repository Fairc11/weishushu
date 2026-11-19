"""release_check.py 回归测试。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import release_check


STANDARD_MIT_LICENSE = """MIT License

Copyright (c) 2026 Weishushu contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class ReleaseCheckTests(unittest.TestCase):

    def test_license_check_accepts_standard_mit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "LICENSE").write_text(STANDARD_MIT_LICENSE, encoding="utf-8")

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_24_license_and_binary_policy()

        self.assertTrue(result.ok, result.msg)

    def test_license_check_rejects_distribution_restriction_in_license(self):
        forbidden_restrictions = [
            "不发布 GitHub Release",
            "禁止分发",
            "官方构建产物",
        ]
        for restriction in forbidden_restrictions:
            with self.subTest(restriction=restriction), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "LICENSE").write_text(
                    f"{STANDARD_MIT_LICENSE}\n{restriction}\n",
                    encoding="utf-8",
                )

                with patch("scripts.release_check.ROOT", root):
                    result = release_check.check_24_license_and_binary_policy()

                self.assertFalse(result.ok, restriction)

    def test_license_check_rejects_noncommercial_addendum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "LICENSE").write_text(
                f"{STANDARD_MIT_LICENSE}\n仅允许个人非商业使用\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_24_license_and_binary_policy()

        self.assertFalse(result.ok)
        self.assertIn("标准 MIT 正文", result.msg)

    def test_license_check_rejects_missing_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_24_license_and_binary_policy()

        self.assertFalse(result.ok)
        self.assertIn("LICENSE", result.msg)

    def test_debug_gate_accepts_helper_that_forces_frozen_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "desktop_app.py").write_text(
                "def should_enable_debug(is_frozen):\n"
                "    if is_frozen:\n"
                "        return False\n"
                "    return True\n"
                "debug_enabled = should_enable_debug(is_frozen)\n"
                "webview.start(debug=debug_enabled)\n",
                encoding="utf-8",
            )
            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_31_desktop_debug_off_when_frozen()

        self.assertTrue(result.ok)

    def test_tests_check_fails_when_pytest_returncode_nonzero_even_with_many_passed(self):
        """pytest 输出含 failed 时，即使 passed 数达标也必须判 FAIL。"""
        class Result:
            returncode = 1
            stdout = "1 failed, 198 passed in 30.71s"
            stderr = ""

        with patch("scripts.release_check.subprocess.run", return_value=Result()):
            result = release_check.check_19_tests_pass()

        self.assertFalse(result.ok)
        self.assertIn("failed", result.msg)

    def test_tests_check_uses_full_suite_with_timeout_600(self):
        """check_19 必须真实运行完整 tests/ 且超时为600秒。"""
        calls = {}

        class Result:
            returncode = 0
            stdout = "199 passed in 20.00s"
            stderr = ""

        def fake_run(*args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            return Result()

        with patch(
            "scripts.release_check.subprocess.run",
            side_effect=fake_run,
        ):
            result = release_check.check_19_tests_pass()

        self.assertTrue(result.ok)
        self.assertEqual(calls["kwargs"]["timeout"], 600)
        self.assertEqual(calls["args"][0][0:5], [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "-q",
        ])

    def test_tests_check_timeout_expired_reports_600_seconds(self):
        """TimeoutExpired不能回退到截断异常，必须明确包含600秒。"""
        with patch(
            "scripts.release_check.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=[sys.executable, "-m", "pytest", "tests/"],
                timeout=600,
            ),
        ):
            result = release_check.check_19_tests_pass()

        self.assertFalse(result.ok)
        self.assertIn("600", result.msg)
        self.assertIn("超时", result.msg)

    def test_requirements_check_allows_split_files_with_recursive_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            req_dir = root / "requirements"
            req_dir.mkdir()
            (root / "requirements.txt").write_text("-r requirements/mac.txt\n", encoding="utf-8")
            (req_dir / "common.txt").write_text("fastapi==0.136.1\n", encoding="utf-8")
            (req_dir / "mac.txt").write_text("-r common.txt\npywebview==6.2.1\n", encoding="utf-8")
            (req_dir / "windows.txt").write_text(
                "-r common.txt\npywebview==6.2.1\npywin32==311\ncomtypes==1.4.16\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_28_requirements_pinned()

        self.assertTrue(result.ok)

    def test_requirements_check_fails_on_unpinned_split_file_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            req_dir = root / "requirements"
            req_dir.mkdir()
            (root / "requirements.txt").write_text("-r requirements/mac.txt\n", encoding="utf-8")
            (req_dir / "common.txt").write_text("fastapi\n", encoding="utf-8")
            (req_dir / "mac.txt").write_text("-r common.txt\npywebview==6.2.1\n", encoding="utf-8")
            (req_dir / "windows.txt").write_text(
                "-r common.txt\npywebview==6.2.1\npywin32==311\ncomtypes==1.4.16\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_28_requirements_pinned()

        self.assertFalse(result.ok)
        self.assertIn("requirements/common.txt:L1: fastapi", result.msg)

    def test_macos_build_check_accepts_build_mac_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build_mac.spec").write_text("# mac build\n" * 30, encoding="utf-8")

            with patch("scripts.release_check.ROOT", root), \
                 patch("scripts.release_check.sys.platform", "darwin"):
                result = release_check.check_12_build_spec_exists()

        self.assertTrue(result.ok)
        self.assertIn("build_mac.spec", result.name)

    def test_jsapi_cookie_path_check_accepts_platform_paths_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend" / "app").mkdir(parents=True)
            (root / "weibo_book").mkdir()
            (root / "scripts").mkdir()
            (root / "js_api.py").write_text(
                "from backend.app.platform_paths import cookie_file_candidates\n",
                encoding="utf-8",
            )
            (root / "backend" / "app" / "platform_paths.py").write_text(
                "from weibo_book.login import get_cookie_file_path\n"
                "def cookie_file_candidates():\n"
                "    return [get_cookie_file_path()]\n",
                encoding="utf-8",
            )
            (root / "weibo_book" / "login.py").write_text(
                "DEFAULT_COOKIE_FILE = '.weibo_book_cookies'\n",
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(
                ".weibo_book_cookies\n.weibo_book_cookies_dev\n",
                encoding="utf-8",
            )
            (root / "scripts" / "create_public_export.py").write_text(
                "SENSITIVE_LOGIN_FILE_NAMES = frozenset({\n"
                "    '.weibo_book_cookies',\n"
                "    '.weibo_book_cookies_dev',\n"
                "    'Cookie',\n"
                "    'Cookies',\n"
                "})\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_32_jsapi_cookie_path_unified()

        self.assertTrue(result.ok)

    def test_jsapi_cookie_path_check_rejects_missing_platform_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend" / "app").mkdir(parents=True)
            (root / "weibo_book").mkdir()
            (root / "scripts").mkdir()
            (root / "js_api.py").write_text("pass\n", encoding="utf-8")
            (root / "backend" / "app" / "platform_paths.py").write_text("pass\n", encoding="utf-8")
            (root / "weibo_book" / "login.py").write_text(
                "DEFAULT_COOKIE_FILE = '.weibo_book_cookies'\n",
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(
                ".weibo_book_cookies\n.weibo_book_cookies_dev\n",
                encoding="utf-8",
            )
            (root / "scripts" / "create_public_export.py").write_text(
                "SENSITIVE_LOGIN_FILE_NAMES = frozenset({\n"
                "    '.weibo_book_cookies',\n"
                "    '.weibo_book_cookies_dev',\n"
                "    'Cookie',\n"
                "    'Cookies',\n"
                "})\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_32_jsapi_cookie_path_unified()

        self.assertFalse(result.ok)

    def test_jsapi_cookie_path_check_rejects_missing_dev_cookie_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend" / "app").mkdir(parents=True)
            (root / "weibo_book").mkdir()
            (root / "scripts").mkdir()
            (root / "js_api.py").write_text(
                "from backend.app.platform_paths import cookie_file_candidates\n",
                encoding="utf-8",
            )
            (root / "backend" / "app" / "platform_paths.py").write_text(
                "from weibo_book.login import get_cookie_file_path\n"
                "def cookie_file_candidates():\n"
                "    return [get_cookie_file_path()]\n",
                encoding="utf-8",
            )
            (root / "weibo_book" / "login.py").write_text(
                "DEFAULT_COOKIE_FILE = '.weibo_book_cookies'\n",
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(
                ".weibo_book_cookies\n",
                encoding="utf-8",
            )
            (root / "scripts" / "create_public_export.py").write_text(
                "SENSITIVE_LOGIN_FILE_NAMES = frozenset({\n"
                "    '.weibo_book_cookies', 'Cookie', 'Cookies'\n"
                "})\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_32_jsapi_cookie_path_unified()

        self.assertFalse(result.ok)
        self.assertIn(".weibo_book_cookies_dev", result.msg)


if __name__ == "__main__":
    unittest.main()
