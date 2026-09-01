"""profile 模块单测：开发版与用户版配置切换。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProfileTests(unittest.TestCase):
    """v2.0.0 阶段 3：profile 切换数据目录、Cookie 文件、Bundle ID、显示名。"""

    def test_user_profile_defaults_when_env_unset(self):
        from backend.app.profile import (
            app_dirname,
            app_display_name,
            app_profile,
            bundle_identifier,
            default_cookie_filename,
            dmg_output_name,
            is_dev_profile,
            window_title,
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app_profile(), "")
            self.assertFalse(is_dev_profile())
            self.assertEqual(app_dirname(), "Weishushu")
            self.assertEqual(app_display_name(), "微书薯")
            self.assertEqual(window_title(), "微书薯")
            self.assertEqual(default_cookie_filename(), ".weibo_book_cookies")
            self.assertEqual(bundle_identifier(), "com.weishushu.desktop")
            self.assertEqual(
                dmg_output_name("2.0.0"),
                "Weishushu-v2.0.0-macOS-arm64.dmg",
            )

    def test_dev_profile_switches_all_identities(self):
        from backend.app.profile import (
            app_dirname,
            app_display_name,
            app_profile,
            bundle_identifier,
            default_cookie_filename,
            dmg_output_name,
            is_dev_profile,
            window_title,
        )

        with patch.dict(os.environ, {"WEISHUSHU_PROFILE": "dev"}, clear=True):
            self.assertEqual(app_profile(), "dev")
            self.assertTrue(is_dev_profile())
            self.assertEqual(app_dirname(), "WeishushuDev")
            self.assertEqual(app_display_name(), "微书薯 Dev")
            self.assertEqual(window_title(), "微书薯 Dev（开发版）")
            self.assertEqual(default_cookie_filename(), ".weibo_book_cookies_dev")
            self.assertEqual(bundle_identifier(), "com.weishushu.desktop.dev")
            self.assertEqual(
                dmg_output_name("2.0.0"),
                "WeishushuDev-v2.0.0-macOS-arm64.dmg",
            )

    def test_profile_is_case_insensitive_and_trimmed(self):
        from backend.app.profile import app_profile, is_dev_profile

        with patch.dict(os.environ, {"WEISHUSHU_PROFILE": "  DEV  "}, clear=True):
            self.assertEqual(app_profile(), "dev")
            self.assertTrue(is_dev_profile())

    def test_unknown_profile_falls_back_to_user(self):
        from backend.app.profile import app_dirname, is_dev_profile

        with patch.dict(os.environ, {"WEISHUSHU_PROFILE": "staging"}, clear=True):
            self.assertFalse(is_dev_profile())
            self.assertEqual(app_dirname(), "Weishushu")

    def test_frozen_dev_executable_selects_dev_without_environment(self):
        """Finder 启动开发版时不得依赖构建终端遗留的环境变量。"""
        from backend.app.profile import (
            app_dirname,
            app_profile,
            default_cookie_filename,
            window_title,
        )

        executable = "/Applications/WeishushuDev.app/Contents/MacOS/WeishushuDev"
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", executable):
            self.assertEqual(app_profile(), "dev")
            self.assertEqual(app_dirname(), "WeishushuDev")
            self.assertEqual(default_cookie_filename(), ".weibo_book_cookies_dev")
            self.assertEqual(window_title(), "微书薯 Dev（开发版）")

    def test_frozen_user_executable_stays_on_personal_profile(self):
        """日常个人版必须继续使用原用户路径和登录文件。"""
        from backend.app.profile import (
            app_dirname,
            app_profile,
            default_cookie_filename,
            window_title,
        )

        executable = "/Applications/Weishushu.app/Contents/MacOS/Weishushu"
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", executable):
            self.assertEqual(app_profile(), "")
            self.assertEqual(app_dirname(), "Weishushu")
            self.assertEqual(default_cookie_filename(), ".weibo_book_cookies")
            self.assertEqual(window_title(), "微书薯")

    def test_frozen_release_executables_ignore_external_dev_profile(self):
        """正式 frozen 身份只由可执行文件决定，不接受运行环境切换。"""
        from backend.app.profile import (
            app_dirname,
            app_profile,
            default_cookie_filename,
        )

        executables = (
            "/Applications/Weishushu.app/Contents/MacOS/Weishushu",
            "C:/Program Files/Weishushu/Weishushu.exe",
        )
        for executable in executables:
            with self.subTest(executable=executable), \
                 patch.dict(os.environ, {"WEISHUSHU_PROFILE": "dev"}, clear=True), \
                 patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", executable):
                self.assertEqual(app_profile(), "")
                self.assertEqual(app_dirname(), "Weishushu")
                self.assertEqual(default_cookie_filename(), ".weibo_book_cookies")

    def test_source_entry_configures_dev_profile_by_default(self):
        """源码入口必须在加载桌面应用前主动隔离开发登录态。"""
        from backend.app.profile import app_profile, configure_source_profile

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "frozen", False, create=True):
            configure_source_profile()
            self.assertEqual(app_profile(), "dev")

    def test_source_entry_does_not_allow_unknown_profile_to_reach_personal_data(self):
        """未知环境值不得使源码开发态回落到日常个人版。"""
        from backend.app.profile import app_profile, configure_source_profile

        with patch.dict(os.environ, {"WEISHUSHU_PROFILE": "staging"}, clear=True), \
             patch.object(sys, "frozen", False, create=True):
            configure_source_profile()
            self.assertEqual(app_profile(), "dev")

    def test_run_main_configures_profile_before_desktop_main(self):
        """`python run.py` 必须在桌面层首次读取路径前完成 profile 配置。"""
        import run
        from backend.app.profile import app_profile

        desktop_app = SimpleNamespace(main=app_profile)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "frozen", False, create=True), \
             patch.dict(sys.modules, {"desktop_app": desktop_app}):
            self.assertEqual(run.main(), "dev")


if __name__ == "__main__":
    unittest.main()
