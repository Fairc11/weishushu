import json
import re
from pathlib import Path


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "following_archive"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _walk(value):
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_following_fixture_requests_and_pagination_are_exact():
    first = _load("following_page_1.json")
    final = _load("following_page_2.json")

    assert first["source"]["request"] == {
        "path": "/ajax/profile/followContent",
        "query": {"sortType": "all", "page": 1},
    }
    assert final["source"]["request"] == {
        "path": "/ajax/profile/followContent",
        "query": {"sortType": "all", "page": 2, "next_cursor": 50},
    }
    assert first["observation"]["original_item_count"] == 50
    assert final["observation"]["original_item_count"] == 41
    assert first["response"]["data"]["follows"]["next_cursor"] == 50
    assert final["response"]["data"]["follows"]["next_cursor"] == 0
    assert final["response"]["data"]["follows"]["has_filtered_attentions"] is True


def test_following_user_sample_preserves_identity_and_registration_fields():
    first = _load("following_page_1.json")
    user = first["response"]["data"]["follows"]["users"][0]

    assert isinstance(user["id"], int)
    assert isinstance(user["idstr"], str)
    assert user["profile_url"] == "10000000001"
    assert re.fullmatch(
        r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{2}:\d{2}:\d{2} \+0800 \d{4}",
        user["created_at"],
    )
    assert "followed_at" not in user
    assert "follow_days" not in user


def test_followed_supertopic_fixture_preserves_stable_link_fields():
    document = _load("followed_supertopics_page_1.json")
    topic = document["response"]["data"]["list"][0]

    assert document["source"]["request"] == {
        "path": "/ajax/profile/topicContent",
        "query": {"tabid": "231093_-_chaohua"},
    }
    assert document["observation"]["original_item_count"] == 7
    assert document["response"]["data"]["max_page"] == 1
    assert topic["oid"].startswith("1022:100808")
    assert topic["link"].startswith("//weibo.com/p/100808")
    assert topic["scheme"].startswith("sinaweibo://pageinfo?containerid=100808")
    assert topic["following"] is True
    assert "followed_at" not in topic
    assert "follow_days" not in topic


def test_following_fixtures_do_not_contain_authentication_material():
    forbidden_keys = {
        "authorization",
        "cookie",
        "token",
        "user_token",
        "sub",
        "subp",
        "ssologinstate",
        "scf",
        "alf",
    }

    for path in sorted(FIXTURE_ROOT.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for value in _walk(document):
            if isinstance(value, str):
                assert value.lower() not in forbidden_keys
                assert "bearer " not in value.lower()
