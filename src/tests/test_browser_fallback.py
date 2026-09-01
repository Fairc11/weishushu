"""浏览器兜底抓取模块测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.models import UserInfo


class BrowserFallbackModuleTests(unittest.TestCase):
    def test_browser_post_conversion_keeps_compat_import(self):
        from weibo_book.browser_fallback import browser_post_to_our_post
        from weibo_book.extractor import browser_post_to_our_post as extractor_browser_post_to_our_post

        self.assertIs(extractor_browser_post_to_our_post, browser_post_to_our_post)

        user = UserInfo(uid="123", screen_name="测试用户", avatar_url="avatar")
        post = browser_post_to_our_post(
            {"mid": "m1", "text": "正文", "time": "2026-01-01", "likes": "1,234"},
            user,
        )

        self.assertIsNotNone(post)
        self.assertEqual(post.bid, "m1")
        self.assertEqual(post.uid, "123")
        self.assertEqual(post.user_name, "测试用户")
        self.assertEqual(post.user_avatar, "avatar")
        self.assertEqual(post.likes_count, 1234)
        self.assertEqual(post.source, "浏览器兜底")

    def test_browser_user_agent_matches_target_platform(self):
        from weibo_book.browser_fallback import get_browser_user_agent

        mac_ua = get_browser_user_agent(platform="darwin")
        win_ua = get_browser_user_agent(platform="win32")
        linux_ua = get_browser_user_agent(platform="linux")

        self.assertIn("Macintosh", mac_ua)
        self.assertNotIn("Windows NT", mac_ua)
        self.assertIn("Windows NT", win_ua)
        self.assertIn("X11", linux_ua)


if __name__ == "__main__":
    unittest.main()
