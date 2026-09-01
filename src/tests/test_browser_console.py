"""v1.2.0 收口 A: 浏览器控制台 5 按钮单测。

覆盖：
- refresh_browser: evaluate_js 调 window.location.reload()
- close_browser_window: destroy + 清空 _browser_window
- get_browser_current_url: evaluate_js 拿 location.href
- browser_back/forward: evaluate_js 调 history.back/forward
- _browser_window=None 时的兜底返错
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BrowserConsoleApiTests(unittest.TestCase):
    """v1.2.0 收口 A: js_api 浏览器控制台 5 按钮。"""

    def setUp(self):
        from js_api import JsApi
        self.api = JsApi()
        # mock 浏览器窗口
        self.mock_window = MagicMock()
        self.api._browser_window = self.mock_window

    def test_refresh_browser_evaluates_reload(self):
        """refresh_browser → evaluate_js('window.location.reload()')"""
        result = self.api.refresh_browser()
        self.mock_window.evaluate_js.assert_called_once_with("window.location.reload()")
        self.assertTrue(result["ok"])

    def test_close_browser_window_destroys_and_clears(self):
        """close_browser_window → destroy + _browser_window = None"""
        result = self.api.close_browser_window()
        self.mock_window.destroy.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertIsNone(self.api._browser_window)

    def test_get_browser_current_url(self):
        """get_browser_current_url → evaluate_js 拿 location.href"""
        self.mock_window.evaluate_js.return_value = "https://m.weibo.cn/u/123"
        url = self.api.get_browser_current_url()
        self.mock_window.evaluate_js.assert_called_once_with("window.location.href")
        self.assertEqual(url, "https://m.weibo.cn/u/123")

    def test_get_browser_current_url_empty(self):
        """evaluate_js 返 None/空 → get_browser_current_url 返 None"""
        self.mock_window.evaluate_js.return_value = ""
        url = self.api.get_browser_current_url()
        self.assertIsNone(url)

    def test_browser_back_evaluates_history_back(self):
        """browser_back → evaluate_js('window.history.back()')"""
        result = self.api.browser_back()
        self.mock_window.evaluate_js.assert_called_once_with("window.history.back()")
        self.assertTrue(result["ok"])

    def test_browser_forward_evaluates_history_forward(self):
        """browser_forward → evaluate_js('window.history.forward()')"""
        result = self.api.browser_forward()
        self.mock_window.evaluate_js.assert_called_once_with("window.history.forward()")
        self.assertTrue(result["ok"])

    def test_all_actions_fail_when_no_browser_window(self):
        """_browser_window = None 时所有按钮返 ok=false。"""
        from js_api import JsApi
        api = JsApi()  # _browser_window = None
        actions = [
            ("refresh_browser", lambda: api.refresh_browser()),
            ("close_browser_window", lambda: api.close_browser_window()),
            ("browser_back", lambda: api.browser_back()),
            ("browser_forward", lambda: api.browser_forward()),
        ]
        for name, fn in actions:
            r = fn()
            self.assertFalse(r["ok"], f"{name} 应 ok=False")
            self.assertIn("浏览器窗口未打开", r["error"], f"{name} 错误信息不符: {r}")

    def test_actions_handle_evaluate_js_exception(self):
        """evaluate_js 抛异常时返 ok=false。"""
        from js_api import JsApi
        api = JsApi()
        mock_window = MagicMock()
        mock_window.evaluate_js.side_effect = Exception("模拟 evaluate_js 失败")
        api._browser_window = mock_window

        for fn, name in [
            (api.refresh_browser, "refresh_browser"),
            (api.browser_back, "browser_back"),
            (api.browser_forward, "browser_forward"),
        ]:
            r = fn()
            self.assertFalse(r["ok"], f"{name} 应 ok=False")
            self.assertIn("模拟 evaluate_js 失败", r["error"], f"{name}: {r}")


class BrowserConsoleRenderTests(unittest.TestCase):
    """v1.2.0 收口 A: index 页面渲染浏览器控制台面板。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.app.main import app
        cls.client = TestClient(app)

    def test_browser_panel_in_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # 面板容器
        self.assertIn("browser-panel", r.text)
        # 5+ 按钮 ID
        for btn_id in [
            "btn-browser-back", "btn-browser-forward", "btn-browser-refresh",
            "btn-browser-sync", "btn-browser-copy", "btn-browser-close",
        ]:
            self.assertIn(btn_id, r.text, f"按钮 #{btn_id} 未渲染")
        # URL 显示区
        self.assertIn("browser-current-url", r.text)


if __name__ == "__main__":
    unittest.main()
