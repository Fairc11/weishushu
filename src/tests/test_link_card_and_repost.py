"""引用卡片与转发微博的离线归档契约。"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from weibo_book.generator import BookGenerator
from weibo_book.media import MediaDownloader
from weibo_book.models import MediaType, Post, PostMedia, UserInfo
from weibo_book.post_converter import crawl_post_to_our_post
from weibo_book.raw_status import RAW_STATUS_KEY


def _crawl_post(*, bid="SYNTHLINK1", raw=None, retweeted=None):
    post = MagicMock()
    post.bid = bid
    post.user_id = "10001"
    post.text = "外层正文"
    post.created_at = datetime(2026, 7, 13, 23, 8)
    post.source = "iPhone"
    post.pic_urls = []
    post.video_url = ""
    post.reposts_count = 0
    post.comments_count = 0
    post.attitudes_count = 0
    post.is_original = retweeted is None
    post.retweeted_status = retweeted
    post.location = ""
    post.raw_data = {RAW_STATUS_KEY: raw or {}}
    return post


def _user():
    return UserInfo(uid="10001", screen_name="测试用户", avatar_url="")


def test_webpage_page_info_becomes_exact_link_card():
    raw = {
        "page_info": {
            "object_type": 2,
            "type": "webpage",
            "page_pic": {"url": "https://i0.hdslb.com/card.jpg"},
            "page_url": "https://weibo.cn/sinaurl?u=https%3A%2F%2Fshare.b23.tv%2Fvideo",
            "url_ori": "http://t.cn/AXKJmpP4",
            "page_title": "BML制作指挥部",
            "content1": "92.6万播放 9.5万点赞 1771弹幕",
        }
    }

    converted = crawl_post_to_our_post(_crawl_post(raw=raw), "10001")

    assert converted.link_card.title == "BML制作指挥部"
    assert converted.link_card.description == "92.6万播放 9.5万点赞 1771弹幕"
    assert converted.link_card.image_url == "https://i0.hdslb.com/card.jpg"
    assert converted.link_card.url == raw["page_info"]["page_url"]
    assert converted.link_card.original_url == "http://t.cn/AXKJmpP4"
    assert converted.link_card.type == "webpage"


def test_retweeted_status_keeps_exact_author_and_nested_card():
    nested_raw = {
        "user": {
            "id": 123,
            "screen_name": "原博主",
            "profile_image_url": "https://wx1.sinaimg.cn/avatar.jpg",
            "verified": True,
            "gender": "f",
        },
        "page_info": {
            "type": "search_topic",
            "page_pic": {"url": "https://wx1.sinaimg.cn/topic.jpg"},
            "page_url": "https://m.weibo.cn/search?containerid=topic",
            "page_title": "#话题#",
            "content1": "10讨论 20阅读",
        },
    }
    nested = _crawl_post(bid="INNER", raw=nested_raw)
    nested.user_id = "123"
    nested.text = "原微博正文"

    converted = crawl_post_to_our_post(
        _crawl_post(bid="OUTER", raw={"user": {"screen_name": "转发者"}}, retweeted=nested),
        "10001",
    )

    assert converted.retweeted.user_name == "原博主"
    assert converted.retweeted.user_avatar == "https://wx1.sinaimg.cn/avatar.jpg"
    assert converted.retweeted.verified is True
    assert converted.retweeted.gender == "f"
    assert converted.retweeted.link_card.title == "#话题#"


def test_html_renders_link_card_and_full_retweet_content():
    post = Post(
        bid="OUTER", uid="1", user_name="转发者", user_avatar="", text="转发理由",
        retweeted=Post(
            bid="INNER", uid="2", user_name="原博主", user_avatar="", text="原微博正文",
            media=[PostMedia(type=MediaType.IMAGE, url="https://wx1.sinaimg.cn/original.jpg")],
        ),
    )
    post.retweeted.link_card = SimpleNamespace(
        title="#话题#", description="10讨论 20阅读", image_url="https://wx1.sinaimg.cn/topic.jpg",
        local_image=None, url="https://m.weibo.cn/search?containerid=topic", original_url="", type="search_topic",
    )

    html = BookGenerator(Path.cwd() / ".run" / "test-link-render").env.get_template("card.html").render(post=post)

    assert 'class="link-card"' in html
    assert "#话题#" in html
    assert 'class="retweeted-username">@原博主' in html
    assert "原微博正文" in html
    assert "https://wx1.sinaimg.cn/original.jpg" in html


def test_downloader_archives_nested_media_and_link_card_image(tmp_path):
    nested = Post(
        bid="INNER", uid="2", user_name="原博主", user_avatar="", text="原微博",
        media=[PostMedia(type=MediaType.IMAGE, url="https://wx1.sinaimg.cn/original.jpg")],
    )
    nested.link_card = SimpleNamespace(
        title="卡片", description="", image_url="https://wx1.sinaimg.cn/card.jpg", local_image=None,
        url="https://example.com", original_url="", type="webpage",
    )
    outer = Post(bid="OUTER", uid="1", user_name="转发者", user_avatar="", text="", retweeted=nested)

    def write_success(_client, _url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")
        return True

    with patch("weibo_book.media.download_file", side_effect=write_success):
        result = MediaDownloader(tmp_path, max_workers=1).download_all([outer])

    assert result["total"] == 2
    assert result["success"] == 2
    assert Path(nested.media[0].local_path).name == "INNER_img_01.jpg"
    assert Path(nested.link_card.local_image).name == "INNER_link.jpg"
