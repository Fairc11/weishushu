"""crawl4weibo 解析边界必须保留微博原始状态。"""

from crawl4weibo.utils.parser import WeiboParser

from weibo_book.extractor import RAW_STATUS_KEY, preserve_raw_status


def test_preserve_raw_status_adds_exact_original_mapping():
    parser = WeiboParser()
    preserve_raw_status(parser)
    status = {
        "id": "1",
        "bid": "abc",
        "user": {"id": "2"},
        "mix_media_info": {"items": [{"type": "pic", "data": {"id": "p1"}}]},
    }

    parsed = parser._parse_single_post(status)

    assert parsed is not None
    assert parsed[RAW_STATUS_KEY] is status


def test_preserve_raw_status_is_idempotent():
    parser = WeiboParser()
    preserve_raw_status(parser)
    wrapped = parser._parse_single_post
    preserve_raw_status(parser)

    assert parser._parse_single_post is wrapped
