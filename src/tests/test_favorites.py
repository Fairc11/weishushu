"""Favorites 提取单测（mock 网络）。覆盖 18 业务方法中 0 覆盖的 get_favorites。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.extractor import WeiboExtractor
from weibo_book.models import ExtractType, ImageQuality, UserInfo


def _make_mock_session(pages_data):
    """pages_data: list[dict]，每页一个响应；用 page=N 取第 N-1 个"""
    session = MagicMock()
    def get(url, params=None, timeout=None):
        page = (params or {}).get("page", 1)
        data = pages_data[page - 1] if page - 1 < len(pages_data) else {"ok": 0}
        resp = MagicMock()
        resp.json.return_value = data
        return resp
    session.get.side_effect = get
    return session


def _make_mock_client_with_session(session):
    client = MagicMock()
    client.session = session
    return client


class GetFavoritesTests(unittest.TestCase):
    def setUp(self):
        self.ext = WeiboExtractor()
        self.ext._user_info = UserInfo(
            uid="123",
            screen_name="测试用户",
            avatar_url="",
            posts_count=0,
        )

    def test_single_page_normal(self):
        """1 页 2 条收藏 → 拿 2 条 + partial=False"""
        page1 = {
            "ok": 1,
            "data": {"favorites": [
                {"status": {
                    "id": "1", "bid": "abc",
                    "user": {"id": 100, "screen_name": "A", "avatar_hd": ""},
                    "text": "fav 1", "created_at": "2026-01-01",
                    "source": "", "reposts_count": 0, "comments_count": 0,
                    "attitudes_count": 0, "pic_ids": [], "pic_infos": {},
                    "page_info": {}, "is_long_text": False,
                }},
                {"status": {
                    "id": "2", "bid": "def",
                    "user": {"id": 100, "screen_name": "A", "avatar_hd": ""},
                    "text": "fav 2", "created_at": "2026-01-02",
                    "source": "", "reposts_count": 0, "comments_count": 0,
                    "attitudes_count": 0, "pic_ids": [], "pic_infos": {},
                    "page_info": {}, "is_long_text": False,
                }},
            ]},
        }
        self.ext.client = _make_mock_client_with_session(_make_mock_session([page1, {"ok": 0}]))

        posts = self.ext.get_favorites("123", max_posts=0)
        self.assertEqual(len(posts), 2)
        self.assertFalse(self.ext._last_partial)
        self.assertEqual(self.ext._last_pages_total, 1)
        self.assertEqual(self.ext._last_pages_failed, 0)

    def test_empty_first_page(self):
        """第一页 ok=0 → 0 条 + partial=False（不需要重试）"""
        self.ext.client = _make_mock_client_with_session(_make_mock_session([{"ok": 0}]))
        posts = self.ext.get_favorites("123")
        self.assertEqual(len(posts), 0)
        self.assertFalse(self.ext._last_partial)

    def test_page_failure_marks_partial(self):
        """第 1 页 ok=1 拿到 2 条；第 2 页抛异常 → L1 应标 partial=True"""
        page1 = {
            "ok": 1,
            "data": {"favorites": [
                {"status": {
                    "id": "1", "bid": "a",
                    "user": {"id": 100, "screen_name": "A", "avatar_hd": ""},
                    "text": "fav 1", "created_at": "2026-01-01",
                    "source": "", "reposts_count": 0, "comments_count": 0,
                    "attitudes_count": 0, "pic_ids": [], "pic_infos": {},
                    "page_info": {}, "is_long_text": False,
                }},
            ]},
        }
        # 第 2 页 session.get 抛
        session = MagicMock()
        def get(url, params=None, timeout=None):
            page = (params or {}).get("page", 1)
            if page == 1:
                resp = MagicMock()
                resp.json.return_value = page1
                return resp
            raise RuntimeError("网络炸了")
        session.get.side_effect = get
        self.ext.client = _make_mock_client_with_session(session)

        posts = self.ext.get_favorites("123")
        # 拿 1 条 + 标 partial
        self.assertEqual(len(posts), 1)
        self.assertTrue(self.ext._last_partial)
        self.assertEqual(self.ext._last_pages_failed, 1)
        self.assertIn("第2页", self.ext._last_partial_reason)

    def test_max_posts_truncates(self):
        """max_posts=1 → 只拿 1 条就停"""
        page1 = {
            "ok": 1,
            "data": {"favorites": [
                {"status": {
                    "id": str(i), "bid": f"b{i}",
                    "user": {"id": 100, "screen_name": "A", "avatar_hd": ""},
                    "text": f"fav {i}", "created_at": "2026-01-01",
                    "source": "", "reposts_count": 0, "comments_count": 0,
                    "attitudes_count": 0, "pic_ids": [], "pic_infos": {},
                    "page_info": {}, "is_long_text": False,
                }} for i in range(5)
            ]},
        }
        self.ext.client = _make_mock_client_with_session(_make_mock_session([page1]))
        posts = self.ext.get_favorites("123", max_posts=1)
        self.assertEqual(len(posts), 1)
        self.assertFalse(self.ext._last_partial)


class FavoritesFetcherModuleTests(unittest.TestCase):
    def test_fetch_favorites_returns_posts_and_state(self):
        from weibo_book.extractor import crawl_post_to_our_post
        from weibo_book.favorites_fetcher import fetch_favorites

        page1 = {
            "ok": 1,
            "data": {"favorites": [
                {"status": {
                    "id": "1", "bid": "abc",
                    "user": {"id": 100, "screen_name": "A", "avatar_hd": "avatar-a"},
                    "text": "fav 1", "created_at": "2026-01-01",
                    "source": "", "reposts_count": 0, "comments_count": 0,
                    "attitudes_count": 0, "pic_ids": [], "pic_infos": {},
                    "page_info": {}, "is_long_text": False,
                }},
            ]},
        }
        client = _make_mock_client_with_session(_make_mock_session([page1, {"ok": 0}]))

        result = fetch_favorites(
            client,
            uid="123",
            screen_name="测试用户",
            max_posts=0,
            image_quality=ImageQuality.ORIGINAL,
            post_converter=crawl_post_to_our_post,
            sleep_func=lambda _seconds: None,
        )

        self.assertEqual(len(result.posts), 1)
        self.assertEqual(result.posts[0].bid, "abc")
        self.assertEqual(result.posts[0].user_name, "A")
        self.assertEqual(result.posts[0].user_avatar, "avatar-a")
        self.assertFalse(result.partial)
        self.assertEqual(result.pages_total, 1)
        self.assertEqual(result.pages_failed, 0)
        self.assertEqual(result.partial_reason, "")
        self.assertEqual(result.skipped_bids, [])


if __name__ == "__main__":
    unittest.main()
