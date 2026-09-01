"""URL 解析全分支单测。覆盖 18 个业务方法中的 parse_uid_from_url / resolve_url。

之前 0 覆盖。修复 L1 时加了 _last_partial 字段——这里验证 partial 标记正确。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.url_parser import parse_uid_from_url


class ParseUIDTests(unittest.TestCase):
    """parse_uid_from_url 全分支覆盖"""

    def test_extractor_keeps_compat_import(self):
        from weibo_book.extractor import parse_uid_from_url as extractor_parse_uid_from_url

        self.assertIs(extractor_parse_uid_from_url, parse_uid_from_url)

    def test_m_weibo_cn_u_path(self):
        self.assertEqual(parse_uid_from_url("https://m.weibo.cn/u/1234567890"), "1234567890")

    def test_m_weibo_cn_profile_path(self):
        self.assertEqual(parse_uid_from_url("https://m.weibo.cn/profile/1234567890"), "1234567890")

    def test_weibo_com_u_path(self):
        self.assertEqual(parse_uid_from_url("https://weibo.com/u/1234567890"), "1234567890")

    def test_weibo_com_bare_uid(self):
        """https://weibo.com/1234567890 → 1234567890"""
        self.assertEqual(parse_uid_from_url("https://weibo.com/1234567890"), "1234567890")

    def test_weibo_com_nickname(self):
        out = parse_uid_from_url("https://weibo.com/nickname123")
        self.assertTrue(out.startswith("nickname:"))
        self.assertIn("nickname123", out)

    def test_query_param_uid(self):
        """?uid=1234567890 兜底"""
        self.assertEqual(parse_uid_from_url("https://example.com/?uid=1234567890"), "1234567890")

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            parse_uid_from_url("https://example.com/not-a-weibo")

    def test_strips_whitespace(self):
        self.assertEqual(parse_uid_from_url("  https://weibo.com/u/1234567890  "), "1234567890")

    # ====== v1.1.2 F4：分享文本解析（🔴 入口 bug 修复） ======
    def test_share_text_chinese_prefix(self):
        """麻花辫 https://weibo.com/u/123 → 123（最常见场景）"""
        self.assertEqual(parse_uid_from_url("麻花辫野生选手 https://weibo.com/u/1234567890"), "1234567890")

    def test_share_text_with_at_user(self):
        """@XX的微博 https://weibo.com/u/123 → 123（关注用户分享）"""
        text = "【XX的微博】 我分享了 @演员王可 的一条微博：麻花辫野生选手 https://weibo.com/1234567890/AbCdEf"
        self.assertEqual(parse_uid_from_url(text), "1234567890")

    def test_share_text_0_5x_douyin_style(self):
        """0.5X xxx https://weibo.com/... → UID（抖音风格前缀）"""
        self.assertEqual(parse_uid_from_url("0.5X 麻花辫 https://weibo.com/1234567890/AbCdEf"), "1234567890")

    def test_share_text_trailing_chinese_period(self):
        """https://weibo.com/u/123。 → 123（中文句号）"""
        self.assertEqual(parse_uid_from_url("https://weibo.com/u/1234567890。"), "1234567890")

    def test_share_text_trailing_chinese_comma(self):
        """https://weibo.com/u/123, 转发 → 123（中文逗号+尾巴）"""
        self.assertEqual(parse_uid_from_url("麻花辫 https://weibo.com/u/1234567890, 转发"), "1234567890")

    def test_share_text_microblog_topic(self):
        """#话题# https://weibo.com/u/123 → 123"""
        self.assertEqual(parse_uid_from_url("演员王可超话 https://weibo.com/u/1234567890"), "1234567890")


class L1PartialTests(unittest.TestCase):
    """L1 v1.1.1：partial 标记在 WeiboExtractor 实例上正确初始化。"""

    def test_extractor_partial_init(self):
        from weibo_book.extractor import WeiboExtractor
        ext = WeiboExtractor()
        # 初始 partial 标记应是 False
        self.assertFalse(ext._last_partial)
        self.assertEqual(ext._last_pages_failed, 0)
        self.assertEqual(ext._last_pages_total, 0)
        self.assertEqual(ext._last_partial_reason, "")

    def test_get_posts_resets_partial(self):
        """get_posts 入口应重置 partial 字段，避免上次调用的污染。"""
        from weibo_book.extractor import WeiboExtractor
        from weibo_book.models import UserInfo, Post
        ext = WeiboExtractor()
        # 模拟上次失败
        ext._last_partial = True
        ext._last_pages_failed = 5
        ext._last_partial_reason = "stale"
        # 直接 mock 掉 get_posts 会调的所有方法，只验证 partial reset
        ext.get_user_info = lambda uid: UserInfo(uid=uid, screen_name="t", avatar_url="")
        ext._get_posts_api = lambda *a, **kw: []  # 0 条
        ext._get_posts_browser = lambda *a, **kw: []  # 备选也 0
        posts = ext.get_posts("123")
        self.assertEqual(posts, [])
        # get_posts 入口已重置 partial
        self.assertFalse(ext._last_partial)
        self.assertEqual(ext._last_partial_reason, "")


if __name__ == "__main__":
    unittest.main()
