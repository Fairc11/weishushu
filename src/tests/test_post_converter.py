"""帖子与媒体转换模块的边界测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PostConverterModuleTests(unittest.TestCase):
    def test_converter_module_is_the_single_implementation_source(self):
        from weibo_book import extractor, media
        from weibo_book.post_converter import (
            crawl_post_to_our_post,
            extract_media,
            transform_image_url,
        )

        self.assertIs(extractor.transform_image_url, transform_image_url)
        self.assertIs(extractor.extract_media, extract_media)
        self.assertIs(extractor.crawl_post_to_our_post, crawl_post_to_our_post)
        self.assertIs(media.transform_image_url, transform_image_url)


if __name__ == "__main__":
    unittest.main()
