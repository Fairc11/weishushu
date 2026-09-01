"""扫码登录必须通过微博服务端校验，不能只看 Cookie 名。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from weibo_book.login import login_with_qrcode


class QrcodeLoginValidationTests(unittest.TestCase):
    def test_visitor_interstitial_switches_to_mobile_qrcode_page(self):
        page = MagicMock()
        page.url = "https://passport.weibo.com/visitor/visitor?entry=miniblog"

        context = MagicMock()
        context.new_page.return_value = page
        context.cookies.return_value = []

        browser = MagicMock()
        browser.new_context.return_value = context

        playwright = MagicMock()
        playwright.chromium.launch.return_value = browser

        manager = MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False

        clock = iter(range(20))
        with patch("weibo_book.login.sync_playwright", return_value=manager), patch(
            "weibo_book.login.time.time", side_effect=lambda: next(clock)
        ), patch("weibo_book.login.time.sleep"):
            result = login_with_qrcode(login_timeout=2, headless=True)

        self.assertEqual(result, [])
        self.assertEqual(page.goto.call_count, 2)
        page.goto.assert_any_call(
            "https://passport.weibo.cn/signin/login",
            wait_until="domcontentloaded",
            timeout=15000,
        )

    def test_stale_sub_cookie_does_not_finish_login(self):
        stale = [{"name": "SUB", "value": "visitor", "domain": ".weibo.com"}]
        valid = [{"name": "SUB", "value": "account", "domain": ".weibo.com"}]

        page = MagicMock()
        page.url = "https://weibo.com/login"
        page.query_selector.return_value = None

        context = MagicMock()
        context.new_page.return_value = page
        cookie_reads = {"count": 0}

        def read_cookies():
            cookie_reads["count"] += 1
            return stale if cookie_reads["count"] == 1 else valid

        context.cookies.side_effect = read_cookies

        browser = MagicMock()
        browser.new_context.return_value = context

        playwright = MagicMock()
        playwright.chromium.launch.return_value = browser

        manager = MagicMock()
        manager.__enter__.return_value = playwright
        manager.__exit__.return_value = False

        clock = iter(range(20))
        with patch("weibo_book.login.sync_playwright", return_value=manager), patch(
            "weibo_book.login.time.time", side_effect=lambda: next(clock)
        ), patch("weibo_book.login.time.sleep"), patch(
            "weibo_book.login.check_cookies_valid", side_effect=[False, True]
        ) as validate, patch(
            "weibo_book.login.save_cookies", return_value=valid
        ) as save:
            result = login_with_qrcode(login_timeout=15, headless=True)

        self.assertEqual(result, valid)
        self.assertEqual(validate.call_count, 2)
        save.assert_called_once_with(valid, None)


if __name__ == "__main__":
    unittest.main()
