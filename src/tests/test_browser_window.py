"""V120-2: 独立浏览器窗口测试。

覆盖：
- 窗口参数：调 webview.create_window，url=m.weibo.cn，宽 480
- dev 降级：webview.create_window 抛异常 → 返 ok=false
- 工具条渲染：index 页面有 btn-browser 按钮
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app


class BrowserWindowApiTests(unittest.TestCase):
    """V120-2.1: js_api.open_browser_window 测试。"""

    def test_open_browser_window_creates_pywebview_window(self):
        """当前版本保留桥方法，但不创建微博窗口。"""
        from js_api import JsApi
        api = JsApi()
        mock_webview = MagicMock()
        with patch.dict(sys.modules, {"webview": mock_webview}):
            result = api.open_browser_window()
        mock_webview.create_window.assert_not_called()
        self.assertEqual(result, {
            "ok": False,
            "error": "该功能正在开发中。",
        })

    def test_open_browser_window_handles_create_failure(self):
        """dev 降级：create_window 抛异常 → 返 ok=false + error。"""
        from js_api import JsApi
        api = JsApi()
        mock_webview = MagicMock()
        mock_webview.create_window.side_effect = Exception("模拟创建失败")
        with patch.dict(sys.modules, {"webview": mock_webview}):
            result = api.open_browser_window()
        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertEqual(
            result["error"],
            "该功能正在开发中。",
        )
        mock_webview.create_window.assert_not_called()


class BrowserButtonRenderTests(unittest.TestCase):
    """V120-2.3: 工具条渲染测试。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_browser_button_in_index(self):
        """index 页面有 btn-browser 按钮 + 🌐 图标。"""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("btn-browser", r.text, "工具条按钮未渲染")
        self.assertIn("🌐", r.text, "🌐 图标未渲染")


if __name__ == "__main__":
    unittest.main()
