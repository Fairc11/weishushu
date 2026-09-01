"""Chrome 登录导入必须服从当前 profile 的路径隔离。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from weibo_book import chrome_import


def _successful_playwright():
    page = MagicMock()
    page.inner_text.return_value = "已登录主页"
    browser = MagicMock()
    browser.new_page.return_value = page
    browser.cookies.return_value = [
        {
            "name": "SUB",
            "value": "dev-login",
            "domain": ".weibo.cn",
            "path": "/",
        }
    ]
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = False
    return manager, playwright


def test_dev_chrome_import_writes_only_dev_cookie_and_profile_dir(tmp_path):
    manager, playwright = _successful_playwright()
    formal_cookie = tmp_path / ".weibo_book_cookies"
    dev_cookie = tmp_path / ".weibo_book_cookies_dev"
    dev_chrome_profile = tmp_path / "dev-cache" / "chrome-import-profile"

    with patch.dict(
        os.environ,
        {"HOME": str(tmp_path), "WEISHUSHU_PROFILE": "dev"},
        clear=True,
    ), patch.object(sys, "frozen", False, create=True), patch.object(
        chrome_import,
        "_find_chrome",
        return_value="/fake/chrome",
    ), patch.object(
        chrome_import,
        "_is_chrome_running",
        return_value=False,
    ), patch(
        "playwright.sync_api.sync_playwright",
        return_value=manager,
    ), patch.object(
        chrome_import.time,
        "sleep",
        return_value=None,
    ), patch(
        "weibo_book.login.get_cookie_file_path",
        return_value=dev_cookie,
    ), patch(
        "backend.app.platform_paths.PlatformPaths.cache_dir",
        return_value=tmp_path / "dev-cache",
    ):
        chrome_import.import_from_chrome()

    assert dev_cookie.exists()
    assert not formal_cookie.exists()
    stored = json.loads(dev_cookie.read_text(encoding="utf-8"))
    records = stored["cookies"] if isinstance(stored, dict) else stored
    assert records[0]["value"] == "dev-login"
    playwright.chromium.launch_persistent_context.assert_called_once()
    assert (
        Path(playwright.chromium.launch_persistent_context.call_args.args[0])
        == dev_chrome_profile
    )
