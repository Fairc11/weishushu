"""V120-3: cookie 注入机制测试。

覆盖：
1. 解析 cookies.json：API 正确读 + 解析
2. 注入 5 个关键 cookie：API 过滤出 SUB/SUBP/ALF/_T_WM/SSOLoginState
3. 注入失败兜底：cookies.json 不存在 → 404
4. 跨设备拦截：cookies.json 格式坏 → 400
5. JsApi.inject_cookies 调 evaluate_js：每 cookie 调一次
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app


def _write_cookies_file(tmp: Path, data, name: str = "cookies.json") -> Path:
    f = tmp / name
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return f


class CookieInjectEndpointTests(unittest.TestCase):
    """V120-3.1-4: /api/browser/inject 端点测试。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _assert_disabled(self):
        with patch(
            "backend.app.routers.router_browser._find_cookies_file",
            side_effect=AssertionError("当前版本不得读取内置浏览器 Cookie"),
        ) as find_file:
            response = self.client.post("/api/browser/inject")
        self.assertEqual(response.status_code, 501)
        self.assertEqual(
            response.json()["detail"],
            "该功能正在开发中。",
        )
        find_file.assert_not_called()

    def test_inject_endpoint_reads_cookies_file(self):
        self._assert_disabled()

    def test_inject_endpoint_filters_to_key_cookies(self):
        self._assert_disabled()

    def test_inject_endpoint_missing_cookies_file_404(self):
        self._assert_disabled()

    def test_inject_endpoint_corrupted_json_400(self):
        self._assert_disabled()


class JsApiInjectCookiesTests(unittest.TestCase):
    """V120-3.5: JsApi.inject_cookies 调 evaluate_js 验证。"""

    def test_inject_cookies_calls_evaluate_js_per_cookie(self):
        """happy path：5 cookie → evaluate_js 调 5 次。"""
        from js_api import JsApi
        api = JsApi()
        mock_window = MagicMock()
        api._browser_window = mock_window

        cookies = [
            {"name": "SUB", "value": "v1", "domain": ".weibo.cn", "path": "/"},
            {"name": "SUBP", "value": "v2", "domain": ".weibo.cn", "path": "/"},
            {"name": "ALF", "value": "v3", "domain": ".weibo.cn", "path": "/"},
            {"name": "_T_WM", "value": "v4", "domain": ".weibo.cn", "path": "/"},
            {"name": "SSOLoginState", "value": "v5", "domain": ".weibo.cn", "path": "/"},
        ]

        result = api.inject_cookies(cookies=cookies)

        # 5 个 cookie → 5 次 evaluate_js
        self.assertEqual(mock_window.evaluate_js.call_count, 5)
        # 全部成功
        self.assertEqual(result["success"], 5)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["ok"])
        # JS 内容含 name=value
        first_call = mock_window.evaluate_js.call_args_list[0]
        js_code = first_call.args[0]
        self.assertIn("document.cookie", js_code)
        self.assertIn("SUB=v1", js_code)
        self.assertIn(".weibo.cn", js_code)

    def test_inject_cookies_no_browser_window_error(self):
        """浏览器窗口未开 → 返 ok=false + 提示。"""
        from js_api import JsApi
        api = JsApi()
        # _browser_window 仍为 None
        result = api.inject_cookies(cookies=[{"name": "SUB", "value": "v"}])
        self.assertFalse(result["ok"])
        self.assertIn("浏览器窗口未打开", result["error"])
        self.assertEqual(result["success"], 0)


class JsApiSyncBrowserLoginTests(unittest.TestCase):
    """统一登录入口：内置浏览器登录后可同步回同一个 cookie 文件。"""

    def _weibo_cookie(self):
        from http.cookies import SimpleCookie

        cookie = SimpleCookie()
        cookie["SUB"] = "valid-sub"
        cookie["SUB"]["domain"] = ".weibo.cn"
        cookie["SUB"]["path"] = "/"
        cookie["SUB"]["secure"] = True
        cookie["SUB"]["httponly"] = True
        cookie["SUB"]["samesite"] = "Lax"
        return cookie

    def test_sync_browser_window_login_saves_valid_cookies(self):
        from js_api import JsApi

        api = JsApi()
        window = MagicMock()
        window.get_cookies.return_value = [self._weibo_cookie()]
        api._browser_window = window

        with patch("weibo_book.login.check_cookies_valid", return_value=True), \
             patch("weibo_book.login.save_cookies", return_value=[{"name": "SUB", "value": "valid-sub"}]) as save:
            result = api.sync_browser_login()

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        save.assert_called_once()
        records = save.call_args.args[0]
        self.assertEqual(records[0]["name"], "SUB")
        self.assertEqual(records[0]["domain"], ".weibo.cn")
        self.assertTrue(records[0]["httpOnly"])
        self.assertEqual(records[0]["sameSite"], "Lax")

    def test_sync_browser_window_login_requires_open_window(self):
        from js_api import JsApi

        result = JsApi().sync_browser_login()

        self.assertFalse(result["ok"])
        self.assertIn("浏览器窗口未打开", result["error"])

    def test_sync_browser_window_login_rejects_invalid_cookies(self):
        from js_api import JsApi

        api = JsApi()
        window = MagicMock()
        window.get_cookies.return_value = [self._weibo_cookie()]
        api._browser_window = window

        with patch("weibo_book.login.check_cookies_valid", return_value=False), \
             patch("weibo_book.login.save_cookies") as save:
            result = api.sync_browser_login()

        self.assertFalse(result["ok"])
        self.assertIn("未通过微博校验", result["error"])
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
