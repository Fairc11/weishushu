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

WEBVIEW2_DECISION = """# Windows WebView2 v147 技术决策

## 复现背景

Windows 历史路线 A 试图使用 Win32 和 WebView2 双控件，在主窗口内渲染网页。该路线假设可以从已安装的 WebView2 Runtime 目录直接加载 `WebView2Loader.dll`，再通过简单 C 导出调用 `CreateCoreWebView2EnvironmentWithOptions`。

## 精确证据

- Windows 环境已检测到 WebView2 Runtime `147.0.3912.86`。
- Win32 离屏窗口可以创建和销毁。
- 该运行时路径中不存在旧路线假设的 `WebView2Loader.dll`。
- `CreateCoreWebView2EnvironmentWithOptions 不再作为 C 导出`，因此旧路线所需的调用路径不可用。

## 影响

以上证据足以确认，依赖 `comtypes` 与简单 C 导出的旧路线 A 对 v147 不可行。该结论不等于 WebView2 原生宿主永久不可行；使用正式 SDK、.NET 包装或独立原生宿主需要另行设计和验证。

## 决策

- 旧路线 A 不实施，不进入当前主线。
- Windows 链路仅保留历史兼容策略：pywebview 的 WebView2 支持、WebView2 Runtime 注册表检测，以及 `C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application` 与 `C:\\Program Files\\Microsoft\\EdgeWebView\\Application` 文件系统兜底检测。"""

ISOLATION_RULE_TEXT = """## 三端隔离硬规则
backend.app.profile
WEISHUSHU_PROFILE
WeishushuDev
.weibo_book_cookies_dev
defaultDataStore()
nonPersistentDataStore()
不得回退
frozen 只信任可执行文件名
get_cookie_file_path()
chrome-import-profile
旧 Windows Cookie
冲突参数
"""

CLINERULE_ISOLATION_TEXT = """12. ❌ **不突破三端隔离**：
backend.app.profile
WEISHUSHU_PROFILE
WeishushuDev
.weibo_book_cookies_dev
defaultDataStore()
nonPersistentDataStore()
不得回退
frozen 只信任可执行文件名
get_cookie_file_path()
chrome-import-profile
旧 Windows Cookie
冲突参数
"""


class ReleaseCheckTests(unittest.TestCase):
    def test_agents_document_check_accepts_substantial_agents_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(
                "项目规则\n" + ISOLATION_RULE_TEXT + "规则正文\n" * 200,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_17_root_agents_md()

        self.assertTrue(result.ok, result.msg)

    def test_agents_document_check_rejects_missing_isolation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(
                "项目规则\n" + "规则正文\n" * 200,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_17_root_agents_md()

        self.assertFalse(result.ok)
        self.assertIn("WEISHUSHU_PROFILE", result.msg)

    def test_agents_document_check_rejects_legacy_claude_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("旧规则\n" + "规则正文\n" * 200, encoding="utf-8")

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_17_root_agents_md()

        self.assertFalse(result.ok)
        self.assertIn("AGENTS.md", result.name)

    def test_clinerules_check_requires_isolation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".clinerules").write_text(
                "代理规则\n" + CLINERULE_ISOLATION_TEXT + "规则正文\n" * 100,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_22_clinerules_exists()

        self.assertTrue(result.ok, result.msg)

    def test_clinerules_check_accepts_formal_prohibition_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".clinerules").write_text(
                "代理规则\n"
                + CLINERULE_ISOLATION_TEXT.replace("不突破三端隔离", "不得突破三端隔离")
                + "规则正文\n" * 100,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_22_clinerules_exists()

        self.assertTrue(result.ok, result.msg)

    def test_clinerules_check_rejects_missing_isolation_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".clinerules").write_text(
                "代理规则\n" + "规则正文\n" * 100,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_22_clinerules_exists()

        self.assertFalse(result.ok)
        self.assertIn("WEISHUSHU_PROFILE", result.msg)

    def test_agents_document_check_rejects_comment_only_isolation_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comment = (
                "<!-- "
                + " ".join(release_check.THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS)
                + " -->\n"
            )
            (root / "AGENTS.md").write_text(
                "项目规则\n" + comment + "规则正文\n" * 200,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_17_root_agents_md()

        self.assertFalse(result.ok)
        self.assertIn("三端隔离硬规则", result.msg)

    def test_clinerules_check_rejects_comment_only_isolation_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comment = (
                "<!-- "
                + " ".join(release_check.THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS)
                + " -->\n"
            )
            (root / ".clinerules").write_text(
                "代理规则\n" + comment + "规则正文\n" * 100,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_22_clinerules_exists()

        self.assertFalse(result.ok)
        self.assertIn("不突破三端隔离", result.msg)

    def test_core_rules_check_accepts_twelve_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".clinerules").write_text(
                "12 条铁律\n公开 GitHub 仓库\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_26_clinerules_has_core_rules()

        self.assertTrue(result.ok, result.msg)

    def test_core_rules_check_rejects_stale_eleven_rule_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".clinerules").write_text(
                "11 条铁律\n公开 GitHub 仓库\n",
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_26_clinerules_has_core_rules()

        self.assertFalse(result.ok)
        self.assertIn("12 铁律", result.msg)

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

    def test_webview2_decision_check_accepts_dedicated_public_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "windows-webview2-v147.md").write_text(
                WEBVIEW2_DECISION,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertTrue(result.ok, result.msg)

    def test_webview2_decision_check_rejects_old_stage4_plan_as_only_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "v1.2.0-stage4-plan.md").write_text(
                WEBVIEW2_DECISION,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("docs/decisions/windows-webview2-v147.md", result.msg)

    def test_webview2_decision_check_rejects_each_missing_required_marker(self):
        markers = [
            "WebView2Loader.dll",
            "v147",
            "CreateCoreWebView2EnvironmentWithOptions",
            "C 导出",
        ]
        for marker in markers:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                decisions = root / "docs" / "decisions"
                decisions.mkdir(parents=True)
                (decisions / "windows-webview2-v147.md").write_text(
                    WEBVIEW2_DECISION.replace(marker, ""),
                    encoding="utf-8",
                )

                with patch("scripts.release_check.ROOT", root):
                    result = release_check.check_27_v120_stage4_route_b_locked()

                self.assertFalse(result.ok, marker)
                self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_missing_explicit_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = WEBVIEW2_DECISION.replace("不可行", "").replace("不实施", "")
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_opposite_c_export_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = (
                "# Windows WebView2 v147 技术决策\n\n"
                "WebView2Loader.dll 在运行时路径中不存在。\n"
                "CreateCoreWebView2EnvironmentWithOptions 仍作为 C 导出。\n"
                "因此旧路线在 v147 不可行，不实施。\n"
            )
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_both_c_export_conclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = (
                f"{WEBVIEW2_DECISION}\n"
                "CreateCoreWebView2EnvironmentWithOptions 仍作为 C 导出。\n"
            )
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_negated_route_conclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = f"{WEBVIEW2_DECISION}\n旧路线 A 并非不可行，也不会不实施。\n"
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_alternate_c_export_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = (
                f"{WEBVIEW2_DECISION}\n"
                "CreateCoreWebView2EnvironmentWithOptions 事实上仍然作为 C 导出。\n"
            )
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

    def test_webview2_decision_check_rejects_alternate_route_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "docs" / "decisions"
            decisions.mkdir(parents=True)
            text = f"{WEBVIEW2_DECISION}\n旧路线 A 并不是不可行，且后续将实施。\n"
            (decisions / "windows-webview2-v147.md").write_text(
                text,
                encoding="utf-8",
            )

            with patch("scripts.release_check.ROOT", root):
                result = release_check.check_27_v120_stage4_route_b_locked()

        self.assertFalse(result.ok)
        self.assertIn("不是已审查的固定正文", result.msg)

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
