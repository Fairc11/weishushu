"""F2 v1.1.2 媒体抓取补全单测。覆盖 9 图 + 1 视频 + 1 实况 边界。"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.extractor import RAW_STATUS_KEY, extract_media
from weibo_book.models import ImageQuality, MediaType, PostMedia


def _make_pic_info(pic_type: str = "", video_url: str = "", img_url: str = "https://wx1.sinaimg.cn/large/abc.jpg") -> dict:
    """构造一个 pic_infos 单条"""
    info = {
        "largest": {"url": img_url},
        "original": {"url": img_url},
        "width": 1080,
        "height": 1080,
    }
    if pic_type:
        info["type"] = pic_type
    if video_url:
        info["video"] = {"url": video_url, "mp4_url": video_url, "h264_url": video_url, "duration": 3}
        info["video_url"] = video_url
        info["duration"] = 3
    return info


def _make_crawl_post(pic_infos=None, pic_ids=None, video_url="", page_info=None) -> MagicMock:
    """构造一个 CrawlPost mock"""
    post = MagicMock()
    post.raw_data = {
        "pic_infos": pic_infos or {},
        "pic_ids": pic_ids or [],
    }
    if page_info:
        post.raw_data["page_info"] = page_info
    post.video_url = video_url
    post.pic_urls = []
    post.retweeted_status = None
    return post


class ExtractMediaBugFixTests(unittest.TestCase):
    """v1.1.2 F2 修复：9 图+视频+实况 边界正确分类"""

    def test_9_images_returns_9_image(self):
        """9 个纯图 → 9 IMAGE（无 LIVE_PHOTO/VIDEO 误判）"""
        pic_infos = {f"pic_{i}": _make_pic_info(pic_type="", img_url=f"https://wx1.sinaimg.cn/large/abc_{i}.jpg") for i in range(9)}
        pic_ids = list(pic_infos.keys())
        post = _make_crawl_post(pic_infos, pic_ids)
        media = extract_media(post, image_quality=ImageQuality.ORIGINAL)
        self.assertEqual(len(media), 9)
        self.assertTrue(all(m.type == MediaType.IMAGE for m in media))

    def test_9_images_plus_video_returns_10(self):
        """9 图 + 独立 video_url → 9 IMAGE + 1 VIDEO = 10"""
        pic_infos = {f"pic_{i}": _make_pic_info(pic_type="", img_url=f"https://wx1.sinaimg.cn/large/abc_{i}.jpg") for i in range(9)}
        pic_ids = list(pic_infos.keys())
        post = _make_crawl_post(pic_infos, pic_ids, video_url="https://video.weibo.com/abc.mp4")
        media = extract_media(post, image_quality=ImageQuality.ORIGINAL)
        self.assertEqual(len(media), 10)
        types = [m.type for m in media]
        self.assertEqual(types.count(MediaType.IMAGE), 9)
        self.assertEqual(types.count(MediaType.VIDEO), 1)

    def test_live_photo_returns_1_live(self):
        """pic_type='live' → 1 LIVE_PHOTO"""
        pic_infos = {"pic_0": _make_pic_info(pic_type="live", video_url="https://video.weibo.com/live.mp4")}
        post = _make_crawl_post(pic_infos, ["pic_0"])
        media = extract_media(post)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, MediaType.LIVE_PHOTO)

    def test_legacy_livephoto_accepts_string_video_url(self):
        post = _make_crawl_post(
            {"pic_0": {"type": "livephoto", "largest": {"url": "https://media.example/live.jpg"}, "video": "https://media.example/live.mov"}},
            ["pic_0"],
        )

        media = extract_media(post)

        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, MediaType.LIVE_PHOTO)
        self.assertEqual(media[0].url, "https://media.example/live.mov")

    def test_image_with_video_field_not_misclassified(self):
        """**关键回归**：pic_type='image' 但 info 含 'video' 键 → 仍归 IMAGE（v1.1.1 会误判为 LIVE_PHOTO）"""
        pic_infos = {
            "pic_0": {
                "type": "image",  # 显式标 image，不是 live
                "video": {"url": "https://video.weibo.com/fake.mp4"},  # 但带 video 字段
                "largest": {"url": "https://wx1.sinaimg.cn/large/abc.jpg"},
                "original": {"url": "https://wx1.sinaimg.cn/original/abc.jpg"},
            }
        }
        post = _make_crawl_post(pic_infos, ["pic_0"])
        media = extract_media(post)
        # v1.1.2：pic_type 优先 → 归 IMAGE（v1.1.1 会归 LIVE_PHOTO）
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, MediaType.IMAGE)

    def test_video_type_with_video_field(self):
        """pic_type='video' 且带 video 字段 → 1 VIDEO"""
        pic_infos = {
            "pic_0": _make_pic_info(
                pic_type="video",
                video_url="https://video.weibo.com/v.mp4",
                img_url="https://wx1.sinaimg.cn/large/cover.jpg",
            )
        }
        post = _make_crawl_post(pic_infos, ["pic_0"])
        media = extract_media(post)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].type, MediaType.VIDEO)

    def test_page_info_video_fallback(self):
        """pic_infos 空 + page_info.type='video' → 1 VIDEO（兜底分支）"""
        page_info = {
            "type": "video",
            "media_info": {
                "mp4_h265_url": "https://video.weibo.com/fallback.mp4",
                "duration": 60,
            },
            "page_pic": {"url": "https://wx1.sinaimg.cn/large/cover.jpg"},
        }
        post = _make_crawl_post(pic_infos={}, pic_ids=[], page_info=page_info)
        # 注意：没有 crawl_post.video_url 也不触发独立 video 分支
        media = extract_media(post)
        # 兜底分支会追加 1 VIDEO
        self.assertGreaterEqual(len(media), 1)
        video_media = [m for m in media if m.type == MediaType.VIDEO]
        self.assertGreaterEqual(len(video_media), 1)
        self.assertIn("fallback.mp4", video_media[0].url)

    def test_duplicate_url_dedup(self):
        """同一 URL 出现两次 → seen_urls 去重"""
        pic_infos = {
            "pic_0": _make_pic_info(pic_type="", img_url="https://wx1.sinaimg.cn/large/same.jpg"),
            "pic_1": _make_pic_info(pic_type="", img_url="https://wx1.sinaimg.cn/large/same.jpg"),
        }
        post = _make_crawl_post(pic_infos, ["pic_0", "pic_1"])
        media = extract_media(post)
        # 同样 URL 只保留 1 个
        self.assertEqual(len(media), 1)

    def test_nine_plus_live_plus_video(self):
        """9 图 + 1 实况 + 1 视频 = 11 媒体（用户核心场景）"""
        # 9 个普通图
        pic_infos = {f"pic_{i}": _make_pic_info(pic_type="", img_url=f"https://wx1.sinaimg.cn/large/abc_{i}.jpg") for i in range(8)}
        # 第 9 个位置放实况
        pic_infos["pic_live"] = _make_pic_info(pic_type="live", video_url="https://video.weibo.com/live.mp4", img_url="https://wx1.sinaimg.cn/large/live.jpg")
        pic_ids = list(pic_infos.keys())
        # 1 独立视频
        post = _make_crawl_post(pic_infos, pic_ids, video_url="https://video.weibo.com/main.mp4")
        media = extract_media(post)
        # 8 IMAGE + 1 LIVE_PHOTO + 1 VIDEO = 10
        self.assertEqual(len(media), 10)
        types = [m.type for m in media]
        self.assertEqual(types.count(MediaType.IMAGE), 8)
        self.assertEqual(types.count(MediaType.LIVE_PHOTO), 1)
        self.assertEqual(types.count(MediaType.VIDEO), 1)

    def test_real_eighteen_item_fixture_preserves_order_and_live_positions(self):
        fixture_path = Path(__file__).parent / "fixtures" / "weibo_media_samples.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["eighteen_live_photo"]
        post = _make_crawl_post()
        post.raw_data[RAW_STATUS_KEY] = {"pics": fixture["pics"]}

        media = extract_media(post)

        self.assertEqual(len(media), 18)
        self.assertEqual(
            [index for index, item in enumerate(media, 1) if item.type == MediaType.LIVE_PHOTO],
            [3, 14, 15, 17],
        )
        self.assertEqual(
            [Path(item.thumbnail or item.url).stem for item in media],
            [f"{index:02d}" for index in range(1, 19)],
        )

    def test_real_nine_video_fixture_keeps_all_videos_and_best_quality(self):
        fixture_path = Path(__file__).parent / "fixtures" / "weibo_media_samples.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["nine_videos"]
        post = _make_crawl_post()
        post.raw_data[RAW_STATUS_KEY] = fixture

        media = extract_media(post)

        self.assertEqual(len(media), 9)
        self.assertTrue(all(item.type == MediaType.VIDEO for item in media))
        self.assertEqual(media[0].url, "https://media.example/v01-720.mp4")
        self.assertEqual([item.duration for item in media], [23, 26, 9, 10, 10, 25, 20, 59, 69])

    def test_mix_media_info_preserves_interleaved_picture_video_order(self):
        post = _make_crawl_post()
        post.raw_data[RAW_STATUS_KEY] = {
            "mix_media_info": {
                "items": [
                    {"type": "pic", "data": {"largest": {"url": "https://media.example/01.jpg"}}},
                    {"type": "video", "data": {"page_pic": {"url": "https://media.example/02.jpg"}, "media_info": {"stream_url": "https://media.example/02.mp4"}}},
                    {"type": "pic", "data": {"largest": {"url": "https://media.example/03.jpg"}}},
                ]
            }
        }

        media = extract_media(post)

        self.assertEqual([item.type for item in media], [MediaType.IMAGE, MediaType.VIDEO, MediaType.IMAGE])

    def test_raw_pics_keep_separate_page_info_video(self):
        post = _make_crawl_post()
        post.raw_data[RAW_STATUS_KEY] = {
            "pics": [
                {"large": {"url": "https://media.example/01.jpg"}},
                {"large": {"url": "https://media.example/02.jpg"}},
            ],
            "page_info": {
                "type": "video",
                "page_pic": {"url": "https://media.example/video.jpg"},
                "media_info": {"stream_url_hd": "https://media.example/video.mp4", "duration": 12},
            },
        }

        media = extract_media(post)

        self.assertEqual([item.type for item in media], [MediaType.IMAGE, MediaType.IMAGE, MediaType.VIDEO])


if __name__ == "__main__":
    unittest.main()
