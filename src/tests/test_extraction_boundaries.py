"""提取日期与指定 BID 的边界回归测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from backend.app.schemas import BackupRequest, StartRequest
from weibo_book.browser_fallback import fetch_posts_browser
from weibo_book.errors import OperationCancelled
from weibo_book.extractor import WeiboExtractor
from weibo_book.models import Post, UserInfo


UTC_PLUS_8 = timezone(timedelta(hours=8))


class StartRequestDateValidationTests(unittest.TestCase):
    def test_valid_one_sided_dates_are_accepted(self):
        start_only = StartRequest(url="https://weibo.com/u/1", start_date="2026-07-01")
        end_only = StartRequest(url="https://weibo.com/u/1", end_date="2026-07-31")

        self.assertEqual(start_only.start_date, "2026-07-01")
        self.assertEqual(end_only.end_date, "2026-07-31")

    def test_invalid_start_date_is_rejected_without_end_date(self):
        with self.assertRaisesRegex(ValidationError, "start_date 必须是 YYYY-MM-DD"):
            StartRequest(url="https://weibo.com/u/1", start_date="2026-02-30")

    def test_invalid_end_date_is_rejected_without_start_date(self):
        with self.assertRaisesRegex(ValidationError, "end_date 必须是 YYYY-MM-DD"):
            StartRequest(url="https://weibo.com/u/1", end_date="2026/07/31")

    def test_compact_iso_date_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "start_date 必须是 YYYY-MM-DD"):
            StartRequest(url="https://weibo.com/u/1", start_date="20260731")

    def test_start_date_after_end_date_is_rejected_in_chinese(self):
        with self.assertRaisesRegex(ValidationError, "开始日期不能晚于结束日期"):
            StartRequest(
                url="https://weibo.com/u/1",
                start_date="2026-08-01",
                end_date="2026-07-31",
            )


class BackupRequestScopeValidationTests(unittest.TestCase):
    def test_backup_accepts_one_sided_dates_and_nonempty_post_ids(self):
        start_only = BackupRequest(output_dir="/tmp/backup", max_posts=0, start_date="2026-07-01")
        manual = BackupRequest(output_dir="/tmp/backup", max_posts=0, post_ids=["bid-1"])

        self.assertEqual(start_only.start_date, "2026-07-01")
        self.assertEqual(manual.post_ids, ["bid-1"])

    def test_backup_rejects_invalid_date_order_in_chinese(self):
        with self.assertRaisesRegex(ValidationError, "开始日期不能晚于结束日期"):
            BackupRequest(
                output_dir="/tmp/backup",
                max_posts=0,
                start_date="2026-08-01",
                end_date="2026-07-31",
            )

    def test_backup_rejects_explicit_empty_post_ids(self):
        with self.assertRaisesRegex(ValidationError, "请至少选择一条微博"):
            BackupRequest(output_dir="/tmp/backup", max_posts=0, post_ids=[])


class DateRangeFilteringTests(unittest.TestCase):
    @staticmethod
    def _converted_post(crawl_post, uid, comments, image_quality):
        return Post(
            bid=crawl_post.bid,
            uid=uid,
            user_name="",
            user_avatar="",
            text=crawl_post.bid,
            created_at=crawl_post.created_at,
        )

    def test_api_end_date_includes_whole_day_and_excludes_next_midnight(self):
        extractor = WeiboExtractor()
        extractor._user_info = UserInfo(uid="1", screen_name="测试", avatar_url="")
        extractor._wait_or_cancel = lambda delay: None
        extractor.client = MagicMock()
        extractor.client.get_user_posts.side_effect = [
            [
                SimpleNamespace(bid="midday", created_at=datetime(2026, 6, 30, 12, tzinfo=UTC_PLUS_8), is_original=True),
                SimpleNamespace(bid="late", created_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC_PLUS_8), is_original=True),
                SimpleNamespace(bid="next-day", created_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC_PLUS_8), is_original=True),
            ],
            [],
        ]

        with patch(
            "weibo_book.extractor.crawl_post_to_our_post",
            side_effect=self._converted_post,
        ):
            posts = extractor._get_posts_api(
                "1", 0, False, 0, "hot", None, "2026-06-30", False
            )

        self.assertEqual([post.bid for post in posts], ["midday", "late"])

    def test_api_pages_report_page_and_real_post_count(self):
        extractor = WeiboExtractor()
        extractor._user_info = UserInfo(uid="1", screen_name="测试", avatar_url="")
        extractor._wait_or_cancel = lambda delay: None
        extractor.client = MagicMock()
        extractor.client.get_user_posts.side_effect = [
            [SimpleNamespace(bid="a", created_at=None, is_original=True)],
            [],
        ]
        events = []
        extractor._progress_event_callback = events.append

        with patch("weibo_book.extractor.crawl_post_to_our_post", side_effect=self._converted_post):
            extractor._get_posts_api("1", 0, False, 0, "hot", None, None, False)

        self.assertEqual(events[-1]["current_page"], 1)
        self.assertEqual(events[-1]["current"], 1)
        self.assertIsNone(events[-1]["total"])


class SelectedPostBoundaryTests(unittest.TestCase):
    @staticmethod
    def _crawl_post(bid):
        return SimpleNamespace(bid=bid, created_at=None)

    @staticmethod
    def _converted_post(crawl_post, uid, comments, image_quality):
        return Post(
            bid=crawl_post.bid,
            uid=uid,
            user_name="",
            user_avatar="",
            text=crawl_post.bid,
            comments=comments or [],
        )

    @staticmethod
    def _extractor():
        extractor = WeiboExtractor()
        extractor._user_info = UserInfo(uid="1", screen_name="测试", avatar_url="")
        extractor.client = MagicMock()
        return extractor

    def test_empty_post_ids_returns_without_api_or_browser_fallback(self):
        extractor = self._extractor()
        extractor._get_posts_api = MagicMock()
        extractor._get_posts_browser = MagicMock()

        posts = extractor.get_posts("1", post_ids=[])

        self.assertEqual(posts, [])
        extractor._get_posts_api.assert_not_called()
        extractor._get_posts_browser.assert_not_called()

    def test_comment_failure_keeps_body_and_later_selected_bid(self):
        extractor = self._extractor()
        extractor.client.get_post_by_bid.side_effect = [
            self._crawl_post("b1"),
            self._crawl_post("b2"),
        ]
        extractor.get_post_comments = MagicMock(
            side_effect=[RuntimeError("评论接口失败"), []]
        )

        with patch(
            "weibo_book.extractor.crawl_post_to_our_post",
            side_effect=self._converted_post,
        ):
            posts = extractor.get_posts(
                "1",
                comments=True,
                comments_count=5,
                comments_type="hot",
                post_ids=["b1", "b2"],
            )

        self.assertEqual([post.bid for post in posts], ["b1", "b2"])
        self.assertTrue(extractor._last_partial)
        self.assertEqual(extractor._last_pages_failed, 1)
        self.assertIn("b1", extractor._last_partial_reason)
        self.assertIn("评论提取失败", extractor._last_partial_reason)
        for call in extractor.client.get_post_by_bid.call_args_list:
            self.assertIs(call.kwargs["with_comments"], False)

    def test_body_failure_marks_partial_and_continues(self):
        extractor = self._extractor()
        extractor.client.get_post_by_bid.side_effect = [
            RuntimeError("正文接口失败"),
            self._crawl_post("b2"),
        ]

        with patch(
            "weibo_book.extractor.crawl_post_to_our_post",
            side_effect=self._converted_post,
        ):
            posts = extractor.get_posts("1", post_ids=["b1", "b2"])

        self.assertEqual([post.bid for post in posts], ["b2"])
        self.assertTrue(extractor._last_partial)
        self.assertEqual(extractor._last_pages_failed, 1)
        self.assertIn("b1", extractor._last_partial_reason)
        self.assertIn("正文提取失败", extractor._last_partial_reason)

    def test_selected_post_cancellation_is_not_degraded(self):
        extractor = self._extractor()
        extractor.client.get_post_by_bid.return_value = self._crawl_post("b1")
        extractor.get_post_comments = MagicMock(side_effect=OperationCancelled("任务已取消"))

        with self.assertRaises(OperationCancelled):
            extractor.get_posts("1", comments=True, post_ids=["b1"])


class BrowserDateRangeFilteringTests(unittest.TestCase):
    def test_browser_end_date_uses_the_same_exclusive_boundary(self):
        posts_data = [
            {"mid": "midday", "text": "a", "time": "2026-06-30 12:00", "likes": "0"},
            {"mid": "late", "text": "b", "time": "2026-06-30 23:59", "likes": "0"},
            {"mid": "next-day", "text": "c", "time": "2026-07-01 00:00", "likes": "0"},
        ]

        class FakePage:
            def __init__(self):
                self.query_count = 0

            def goto(self, *args, **kwargs):
                return None

            def evaluate(self, script):
                if script.startswith("() =>"):
                    self.query_count += 1
                    return posts_data if self.query_count == 1 else []
                return None

        class FakeContext:
            def __init__(self):
                self.page = FakePage()

            def new_page(self):
                return self.page

        class FakeBrowser:
            def new_context(self, **kwargs):
                return FakeContext()

            def close(self):
                return None

        class FakeChromium:
            def launch(self, **kwargs):
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakeManager:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, exc_type, exc, traceback):
                return False

        posts = fetch_posts_browser(
            client=SimpleNamespace(),
            user=UserInfo(uid="1", screen_name="测试", avatar_url=""),
            uid="1",
            end_dt=datetime(2026, 7, 1, 0, 0, tzinfo=UTC_PLUS_8),
            sleep_func=lambda delay: None,
            sync_playwright_factory=lambda: FakeManager(),
        )

        self.assertEqual([post.bid for post in posts], ["midday", "late"])


if __name__ == "__main__":
    unittest.main()
