"""预览与登录态的业务契约。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class LoginAndPreviewContractTests(unittest.TestCase):
    """轻量预览应复用静默登录态，不应意外打开扫码窗口。"""

    def test_silent_login_without_cookie_does_not_open_qrcode(self):
        from weibo_book.api import WeiboBook

        with patch("weibo_book.api.load_cookies", return_value={}), \
             patch("weibo_book.api.login_with_qrcode") as login:
            self.assertIsNone(WeiboBook().ensure_login(force=False))
            login.assert_not_called()

    def test_preview_reuses_stored_cookie_and_partial_metadata(self):
        from weibo_book.api import WeiboBook
        from weibo_book.models import ImageQuality

        user = SimpleNamespace(uid="123", screen_name="测试", avatar_url="", posts_count=1, followers_count=2)
        with patch("weibo_book.api.load_cookies", return_value={
            "cookies": [{"name": "SUB", "value": "abc"}],
        }), patch("weibo_book.api.WeiboExtractor") as MockExtractor:
            extractor = MagicMock()
            extractor.resolve_url.return_value = "123"
            extractor.get_user_info.return_value = user
            extractor.preview_posts.return_value = [{"bid": "a"}]
            extractor._last_partial = True
            extractor._last_partial_reason = "预览第 2 页失败"
            MockExtractor.return_value = extractor

            data = WeiboBook(image_quality=ImageQuality.HQ).preview_posts(
                "https://weibo.com/u/123", count=1,
            )

        MockExtractor.assert_called_once_with(cookie_str="SUB=abc", image_quality=ImageQuality.HQ)
        self.assertTrue(data["_partial"])
        self.assertEqual(data["_partial_reason"], "预览第 2 页失败")

    def test_extract_logs_string_account_counts_without_crashing(self):
        """上游账号统计可能是字符串，日志不得用整数占位符触发 TypeError。"""
        from weibo_book.api import WeiboBook

        user = SimpleNamespace(
            uid="123",
            screen_name="测试",
            avatar_url="",
            posts_count="200",
            followers_count="132",
        )
        with patch.object(WeiboBook, "ensure_login", return_value=None), \
             patch("weibo_book.api.WeiboExtractor") as MockExtractor, \
             self.assertLogs("weibo_book.api", level="INFO") as captured:
            extractor = MagicMock()
            extractor.resolve_url.return_value = "123"
            extractor.get_user_info.return_value = user
            extractor.get_extracted.return_value = []
            extractor._last_partial = False
            extractor._last_pages_failed = 0
            extractor._last_pages_total = 1
            extractor._last_partial_reason = ""
            MockExtractor.return_value = extractor

            data = WeiboBook().extract("https://weibo.com/u/123")

        self.assertEqual(data["user"], user)
        self.assertTrue(any("200 条微博 · 132 粉丝" in line for line in captured.output))


class ExtractorPreviewContractTests(unittest.TestCase):
    """预览摘要应保留列表页能显示的互动数和媒体数。"""

    def test_preview_posts_exposes_counts(self):
        from weibo_book.extractor import WeiboExtractor

        extractor = WeiboExtractor.__new__(WeiboExtractor)
        extractor._user_info = object()
        extractor.client = MagicMock()
        extractor.client.get_user_posts.return_value = [SimpleNamespace(
            bid="a", text="测试", created_at=None, pic_urls=["https://img.example/a.jpg"],
            video_url=None, is_original=True, reposts_count=4, comments_count=5,
            attitudes_count=6,
        )]

        preview = extractor.preview_posts("123", count=1)[0]

        self.assertEqual(preview["media_count"], 1)
        self.assertEqual(preview["reposts_count"], 4)
        self.assertEqual(preview["comments_count"], 5)
        self.assertEqual(preview["likes_count"], 6)

    def test_router_keeps_extractor_media_count(self):
        from backend.app.routers.router_scraper import _preview_to_dict

        payload = _preview_to_dict({"bid": "a", "media_count": 3})

        self.assertEqual(payload["media_count"], 3)


class FavoritesRequestContractTests(unittest.TestCase):
    def test_count_zero_means_all(self):
        from backend.app.routers.router_favorites import FavoritesListRequest

        request = FavoritesListRequest.model_validate({
            "url": "https://weibo.com/u/123", "count": 0,
        })

        self.assertEqual(request.count, 0)

    def test_favorites_reuses_stored_cookie(self):
        from backend.app.routers.router_favorites import FavoritesListRequest, list_favorites

        user = SimpleNamespace(uid="123", screen_name="测试", avatar_url="")
        with patch("backend.app.routers.router_favorites.check_profile"), \
             patch("weibo_book.WeiboBook") as MockBook, \
             patch("weibo_book.extractor.WeiboExtractor") as MockExtractor:
            book = MagicMock()
            book.ensure_login.return_value = "SUB=abc"
            MockBook.return_value = book
            extractor = MagicMock()
            extractor.resolve_url.return_value = "123"
            extractor._user_info = user
            extractor.get_favorites.return_value = []
            extractor._last_partial = False
            extractor._last_pages_total = 1
            MockExtractor.return_value = extractor

            result = asyncio.run(list_favorites(
                FavoritesListRequest(url="https://weibo.com/u/123"), MagicMock(),
            ))

        self.assertEqual(result["user"]["uid"], "123")
        MockExtractor.assert_called_once_with(cookie_str="SUB=abc")
