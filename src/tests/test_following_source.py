from __future__ import annotations

import json
from pathlib import Path

import pytest

from weibo_book.errors import WeiboError, WeiboErrorKind


FIXTURES = Path(__file__).parent / "fixtures" / "following_archive"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["response"]


def _blogger_page(*, users: list[dict], total: int, cursor: int, filtered=False):
    return {
        "ok": 1,
        "data": {
            "follows": {
                "users": users,
                "total_number": total,
                "next_cursor": cursor,
                "has_filtered_attentions": filtered,
            }
        },
    }


def _user(identity: str, name: str) -> dict:
    return {"idstr": identity, "screen_name": name}


def test_parsers_keep_fixture_verified_identities_and_links():
    from weibo_book.archive.following_source import (
        parse_blogger_page,
        parse_supertopic_page,
    )

    blogger = parse_blogger_page(_fixture("following_page_1.json"), source_offset=0)
    topic = parse_supertopic_page(
        _fixture("followed_supertopics_page_1.json"), source_offset=0
    )

    assert blogger.items[0].object_id == "1000000001"
    assert blogger.items[0].display_name == "测试博主"
    assert blogger.items[0].page_url == "https://weibo.com/u/1000000001"
    assert blogger.items[0].platform_followed_at == ""
    assert topic.items[0].object_id == "1022:10080800000000000000000000000000000000"
    assert topic.items[0].page_url == "//weibo.com/p/10080800000000000000000000000000000000"
    assert topic.items[0].app_scheme == "sinaweibo://pageinfo?containerid=10080800000000000000000000000000000000"


def test_blogger_source_uses_exact_page_and_cursor_sequence():
    from weibo_book.archive.following_source import FollowingSource

    calls = []
    responses = [
        _blogger_page(users=[_user("1", "甲")], total=2, cursor=50),
        _blogger_page(users=[_user("2", "乙")], total=2, cursor=0),
    ]

    def request(url, params, headers):
        calls.append((url, params, headers))
        return responses.pop(0)

    result = FollowingSource(request, self_uid="9000").fetch_bloggers()

    assert [item.object_id for item in result.items] == ["1", "2"]
    assert result.complete is True
    assert result.reported_total == 2
    assert calls == [
        (
            "https://weibo.com/ajax/profile/followContent",
            {"sortType": "all", "page": 1},
            {"Referer": "https://weibo.com/u/9000"},
        ),
        (
            "https://weibo.com/ajax/profile/followContent",
            {"sortType": "all", "page": 2, "next_cursor": 50},
            {"Referer": "https://weibo.com/u/9000"},
        ),
    ]


def test_blogger_source_rejects_non_numeric_current_identity():
    from weibo_book.archive.following_source import FollowingSource

    with pytest.raises(WeiboError, match="账号标识") as raised:
        FollowingSource(lambda *_args: {}, self_uid="not-numeric")
    assert raised.value.kind is WeiboErrorKind.AUTH


def test_following_source_delegates_to_verified_wake_probe():
    from weibo_book.archive.following_source import FollowingSource

    calls = []
    source = FollowingSource(
        lambda *_args: {},
        self_uid="9000",
        session_probe=lambda: calls.append("probe"),
    )

    source.probe_session()

    assert calls == ["probe"]


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([
            _blogger_page(users=[_user("1", "甲")], total=2, cursor=50),
            _blogger_page(users=[_user("1", "甲")], total=2, cursor=0),
        ], "重复"),
        ([_blogger_page(users=[_user("1", "甲")], total=2, cursor=0)], "报告总数"),
        ([_blogger_page(users=[_user("1", "甲")], total=1, cursor=0, filtered=True)], "过滤"),
    ],
)
def test_blogger_incomplete_results_never_become_complete(responses, message):
    from weibo_book.archive.following_source import FollowingSource

    source = FollowingSource(lambda *_args: responses.pop(0), self_uid="9000")
    with pytest.raises(WeiboError, match=message):
        source.fetch_bloggers()


def test_supertopic_rejects_unverified_additional_pages_and_empty_list():
    from weibo_book.archive.following_source import parse_supertopic_page

    multi = _fixture("followed_supertopics_page_1.json")
    multi["data"]["max_page"] = 2
    with pytest.raises(WeiboError, match="翻页参数"):
        parse_supertopic_page(multi, source_offset=0)

    empty = {"ok": 1, "data": {"list": [], "total_number": 0, "max_page": 1}}
    with pytest.raises(WeiboError, match="空清单"):
        parse_supertopic_page(empty, source_offset=0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": 1, "data": {}},
        _blogger_page(users=[{"idstr": 1, "screen_name": "甲"}], total=1, cursor=0),
        _blogger_page(users=[{"idstr": "not-numeric", "screen_name": "甲"}], total=1, cursor=0),
        _blogger_page(users=[{"idstr": "1", "screen_name": ""}], total=1, cursor=0),
    ],
)
def test_blogger_parser_rejects_unknown_or_invalid_shapes(payload):
    from weibo_book.archive.following_source import parse_blogger_page

    with pytest.raises(WeiboError) as raised:
        parse_blogger_page(payload, source_offset=0)
    assert raised.value.kind is WeiboErrorKind.PARSE


def test_production_request_adapter_forces_single_attempt_and_no_proxy():
    from weibo_book.archive.following_source import CrawlClientFollowingRequest

    calls = []

    class Client:
        def _request(self, url, params, **kwargs):
            calls.append((url, params, kwargs))
            return {"ok": 1}

    adapter = CrawlClientFollowingRequest(Client())
    assert adapter("https://weibo.com/example", {"page": 1}, {"Referer": "x"}) == {"ok": 1}
    assert calls == [(
        "https://weibo.com/example",
        {"page": 1},
        {"max_retries": 1, "use_proxy": False, "headers": {"Referer": "x"}},
    )]
