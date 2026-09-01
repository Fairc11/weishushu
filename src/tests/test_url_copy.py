"""V120-5: 跨窗口复制 URL 测试。

覆盖：
- m.weibo.cn/u/123 复制正确 → 主区 URL 框填上
- 跨域 https://example.com 拒绝 → toast 报错
- 主区收到事件（轮询 get_copied_url 拿到 URL）
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CopyUrlToMainTests(unittest.TestCase):
    """V120-5.1: JsApi.copy_url_to_main 验证 weibo 链接。"""

    def setUp(self):
        from js_api import JsApi
        # 重置类属性（其他测试可能污染）
        JsApi._last_copied_url = None
        self.api = JsApi()

    def test_copy_m_weibo_cn_url_accepted(self):
        """m.weibo.cn/u/123 → ok=True + URL 存到 _last_copied_url。"""
        result = self.api.copy_url_to_main("https://m.weibo.cn/u/1234567890")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://m.weibo.cn/u/1234567890")
        # 类属性被设
        from js_api import JsApi
        self.assertEqual(JsApi._last_copied_url, "https://m.weibo.cn/u/1234567890")

    def test_copy_weibo_com_url_accepted(self):
        """weibo.com/123 兼容 → ok=True。"""
        result = self.api.copy_url_to_main("https://weibo.com/123456")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://weibo.com/123456")

    def test_copy_non_weibo_url_rejected(self):
        """跨域 https://example.com → ok=False + error。"""
        result = self.api.copy_url_to_main("https://example.com/foo")
        self.assertFalse(result["ok"])
        self.assertIn("非微博链接", result["error"])
        # 不存
        from js_api import JsApi
        self.assertIsNone(JsApi._last_copied_url)

    def test_copy_empty_url_rejected(self):
        """空 URL → ok=False。"""
        result = self.api.copy_url_to_main("")
        self.assertFalse(result["ok"])
        self.assertIn("URL 为空", result["error"])

    def test_copy_url_from_browser_window(self):
        """从 _browser_window.evaluate_js 拿 URL。"""
        mock_window = MagicMock()
        mock_window.evaluate_js.return_value = "https://m.weibo.cn/detail/456"
        self.api._browser_window = mock_window

        result = self.api.copy_url_to_main()  # 不传 url

        mock_window.evaluate_js.assert_called_once_with("window.location.href")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://m.weibo.cn/detail/456")

    def test_copy_url_from_browser_window_no_window(self):
        """_browser_window 为 None + 不传 url → ok=False。"""
        # _browser_window 缺省 None
        result = self.api.copy_url_to_main()
        self.assertFalse(result["ok"])
        self.assertIn("浏览器窗口未打开", result["error"])


class GetCopiedUrlTests(unittest.TestCase):
    """V120-5.2: 主区轮询 get_copied_url 拿 URL。"""

    def setUp(self):
        from js_api import JsApi
        JsApi._last_copied_url = None
        self.api = JsApi()

    def test_get_copied_url_returns_and_clears(self):
        """get_copied_url 拿 URL 后清空（一次性消费）。"""
        from js_api import JsApi
        JsApi._last_copied_url = "https://m.weibo.cn/u/789"

        result = self.api.get_copied_url()
        self.assertEqual(result, "https://m.weibo.cn/u/789")
        # 类属性被清
        self.assertIsNone(JsApi._last_copied_url)

        # 第二次拿返 None
        result2 = self.api.get_copied_url()
        self.assertIsNone(result2)

    def test_get_copied_url_empty_returns_none(self):
        """没复制过 → 返 None。"""
        result = self.api.get_copied_url()
        self.assertIsNone(result)


class CopyUrlIntegrationTests(unittest.TestCase):
    """V120-5.3: 完整链路：copy → 主区收到 → 清空。"""

    def test_full_round_trip(self):
        """模拟前端：copy_url_to_main → get_copied_url → URL 框自动填。"""
        from js_api import JsApi
        JsApi._last_copied_url = None
        api = JsApi()

        # 1. 浏览器窗口触发 copy
        copy_result = api.copy_url_to_main("https://m.weibo.cn/u/111")
        self.assertTrue(copy_result["ok"])

        # 2. 主区前端轮询 get_copied_url
        main_url = api.get_copied_url()
        self.assertEqual(main_url, "https://m.weibo.cn/u/111")

        # 3. 主区填到 URL 框（实际是前端逻辑，验证数据流）
        # 4. 下次轮询返 None（已消费）
        self.assertIsNone(api.get_copied_url())


if __name__ == "__main__":
    unittest.main()
