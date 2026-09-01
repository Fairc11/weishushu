"""回归测试：transform_image_url（曾经因 extractor.py / media.py 两份不一致而炸）"""

import sys
import unittest
from pathlib import Path

# 让 weibo_book 能被 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.extractor import transform_image_url
from weibo_book.media import transform_image_url as media_transform
from weibo_book.models import ImageQuality


class TransformImageUrlTests(unittest.TestCase):
    """清零 4 个回归 case：含 mw2000 前缀的源 URL 不能让目标 URL 残留前缀"""

    def test_mw2000_prefix_to_hq(self):
        url = "https://wx1.sinaimg.cn/mw2000/large/abc.jpg"
        out = transform_image_url(url, ImageQuality.HQ)
        self.assertEqual(out, "https://wx1.sinaimg.cn/original/abc.jpg")
        self.assertNotIn("mw2000/", out)

    def test_mw2000_prefix_to_original(self):
        url = "https://wx1.sinaimg.cn/mw2000/large/abc.jpg"
        out = transform_image_url(url, ImageQuality.ORIGINAL)
        self.assertEqual(out, "https://wx1.sinaimg.cn/large/abc.jpg")

    def test_no_prefix_direct_swap(self):
        url = "https://wx1.sinaimg.cn/large/abc.jpg"
        out = transform_image_url(url, ImageQuality.HQ)
        self.assertEqual(out, "https://wx1.sinaimg.cn/original/abc.jpg")

    def test_thumb180_to_original(self):
        url = "https://wx1.sinaimg.cn/thumb180/abc.jpg"
        out = transform_image_url(url, ImageQuality.ORIGINAL)
        self.assertEqual(out, "https://wx1.sinaimg.cn/large/abc.jpg")

    def test_empty_url_passthrough(self):
        self.assertEqual(transform_image_url("", ImageQuality.HQ), "")

    def test_media_module_reuses_extractor_version(self):
        """关键回归：media.py 不能自己实现一份，必须 from extractor import"""
        self.assertIs(media_transform, transform_image_url)


if __name__ == "__main__":
    unittest.main()
