from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from crawl4weibo.models.post import Post as CrawlPost

from weibo_book.errors import OperationCancelled
from weibo_book.extractor import WeiboExtractor
from weibo_book.generator import BookGenerator
from weibo_book.models import Post, UserInfo
from weibo_book.post_converter import crawl_post_to_our_post
from weibo_book.raw_status import RAW_STATUS_KEY
from weibo_book.text_content import weibo_html_to_text


def _crawl_post(*, text="解析器回退正文", raw=None, retweeted=None):
    return SimpleNamespace(
        bid="OUTER",
        user_id="10001",
        text=text,
        created_at=datetime(2026, 7, 26, 12, 0),
        source="",
        pic_urls=[],
        video_url="",
        reposts_count=0,
        comments_count=0,
        attitudes_count=0,
        is_original=retweeted is None,
        retweeted_status=retweeted,
        location="",
        raw_data={RAW_STATUS_KEY: raw} if raw is not None else {},
    )


def test_weibo_html_to_text_preserves_breaks_entities_and_visible_link_text():
    source = (
        "第一行<br>第二行<br><br>"
        '<a href="https://example.invalid/topic">#测试话题#</a>'
        "&amp;结尾\r\n附加行"
    )

    assert weibo_html_to_text(source) == (
        "第一行\n第二行\n\n#测试话题#&结尾\n附加行"
    )


def test_converter_prefers_exact_raw_text_even_when_empty():
    converted = crawl_post_to_our_post(
        _crawl_post(text="不得使用", raw={"text": ""}),
        "10001",
    )

    assert converted.text == ""


def test_converter_falls_back_when_exact_raw_text_is_not_a_string():
    converted = crawl_post_to_our_post(
        _crawl_post(text="解析器回退正文", raw={"text": None}),
        "10001",
    )

    assert converted.text == "解析器回退正文"


def test_api_long_post_uses_detail_raw_text_without_a_second_detail_request():
    list_post = CrawlPost(
        id="LONG",
        bid="LONG",
        user_id="10001",
        text="列表页截断正文",
        created_at=datetime(2026, 7, 26, 12, 0),
        is_long_text=True,
        raw_data={
            RAW_STATUS_KEY: {
                "text": "列表页截断正文",
                "user": {"screen_name": "测试用户"},
            }
        },
    )
    detail_post = CrawlPost(
        id="LONG",
        bid="LONG",
        user_id="10001",
        text="完整正文第一行 完整正文第三行",
        created_at=datetime(2026, 7, 26, 12, 0),
        is_long_text=True,
        raw_data={
            RAW_STATUS_KEY: {
                "text": "完整正文第一行<br><br>完整正文第三行",
                "user": {"screen_name": "测试用户"},
            }
        },
    )
    extractor = WeiboExtractor()
    extractor._user_info = UserInfo(
        uid="10001",
        screen_name="测试用户",
        avatar_url="",
    )
    extractor._wait_or_cancel = lambda _delay: None
    extractor.client = MagicMock()
    extractor.client.get_user_posts.side_effect = [[list_post], []]
    extractor.client.get_post_by_bid.return_value = detail_post

    posts = extractor._get_posts_api(
        "10001",
        0,
        False,
        0,
        "hot",
        None,
        None,
        False,
    )

    assert posts[0].text == "完整正文第一行\n\n完整正文第三行"
    assert extractor.client.get_user_posts.call_args_list[0].kwargs["expand"] is False
    extractor.client.get_post_by_bid.assert_called_once_with(
        "LONG",
        with_comments=False,
    )


def test_api_long_post_expansion_does_not_convert_cancellation_into_page_retry():
    list_post = CrawlPost(
        id="LONG",
        bid="LONG",
        user_id="10001",
        text="列表页正文",
        is_long_text=True,
    )
    extractor = WeiboExtractor()
    extractor._user_info = UserInfo(
        uid="10001",
        screen_name="测试用户",
        avatar_url="",
    )
    extractor._wait_or_cancel = lambda _delay: None
    extractor.client = MagicMock()
    extractor.client.get_user_posts.return_value = [list_post]
    extractor.client.get_post_by_bid.side_effect = OperationCancelled("任务已取消")

    try:
        extractor._get_posts_api(
            "10001",
            0,
            False,
            0,
            "hot",
            None,
            None,
            False,
        )
    except OperationCancelled:
        pass
    else:
        raise AssertionError("长微博详情提取期间的取消操作必须原样抛出")

    assert extractor.client.get_user_posts.call_count == 1


def test_retweet_recursively_uses_exact_raw_text():
    retweeted = _crawl_post(raw={"text": "原微博第一行<br><br>原微博第三行"})
    retweeted.bid = "INNER"
    converted = crawl_post_to_our_post(
        _crawl_post(raw={"text": "转发理由<br>第二行"}, retweeted=retweeted),
        "10001",
    )

    assert converted.text == "转发理由\n第二行"
    assert converted.retweeted is not None
    assert converted.retweeted.text == "原微博第一行\n\n原微博第三行"


def test_generator_outputs_keep_same_multiline_post_text(tmp_path: Path):
    text = "第一段第一行\n第一段第二行\n\n第二段"
    post = Post(
        bid="A",
        uid="10001",
        user_name="测试用户",
        user_avatar="",
        text=text,
    )
    user = UserInfo(uid="10001", screen_name="测试用户", avatar_url="")
    generator = BookGenerator(tmp_path)

    markdown_path = Path(generator.generate_markdown([post], user))
    json_path = Path(generator.generate_json([post], user))
    csv_path = Path(generator.generate_csv([post], user))

    assert text in markdown_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["posts"][0]["text"] == text
    with csv_path.open(encoding="utf-8", newline="") as stream:
        assert next(csv.DictReader(stream))["text"] == text


def test_generator_retweet_markdown_prefixes_every_preserved_line(tmp_path: Path):
    retweeted = Post(
        bid="INNER",
        uid="20002",
        user_name="原博主",
        user_avatar="",
        text="第一行\n\n第三行",
    )
    post = Post(
        bid="OUTER",
        uid="10001",
        user_name="测试用户",
        user_avatar="",
        text="转发理由",
        retweeted=retweeted,
    )
    fragment = BookGenerator(tmp_path)._post_to_markdown(post, 1)

    assert "> 第一行\n> \n> 第三行" in fragment
