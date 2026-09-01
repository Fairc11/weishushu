"""博主搜索与目标识别路由契约（v2.0.1 备份他人微博入口）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.rate_limit import profile_limiter


@pytest.fixture(autouse=True)
def _reset_profile_limiter():
    profile_limiter.reset()
    yield
    profile_limiter.reset()


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _user(uid, name, *, verified=False, reason="", followers="100", posts=10, desc=""):
    return SimpleNamespace(
        id=uid,
        screen_name=name,
        avatar_url=f"https://img.test/{uid}.jpg",
        verified=verified,
        verified_reason=reason,
        followers_count=followers,
        posts_count=posts,
        description=desc,
    )


def _fake_extractor(users=None, info=None):
    extractor = MagicMock()
    extractor.client.search_users.return_value = users or []
    extractor.resolve_url.return_value = "20002"
    extractor.get_user_info.return_value = info or SimpleNamespace(
        uid="20002",
        screen_name="目标博主",
        avatar_url="https://img.test/20002.jpg",
        verified=True,
        verified_reason="测试认证",
        followers_count="1.2万",
        posts_count=256,
        description="简介",
    )
    return extractor


def test_search_users_returns_verified_first(client):
    users = [
        _user("30003", "普通用户甲"),
        _user("20002", "认证博主", verified=True, reason="测试认证", followers="1.2万"),
        _user("40004", "普通用户乙"),
    ]
    extractor = _fake_extractor(users=users)
    with patch(
        "backend.app.routers.router_search._build_extractor",
        return_value=extractor,
    ):
        response = client.post("/api/search/users", json={"query": "博主"})
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [r["uid"] for r in results] == ["20002", "30003", "40004"]
    first = results[0]
    assert first["verified"] is True
    assert first["verified_reason"] == "测试认证"
    assert first["screen_name"] == "认证博主"


def test_search_users_empty_result_is_ok(client):
    extractor = _fake_extractor(users=[])
    with patch(
        "backend.app.routers.router_search._build_extractor",
        return_value=extractor,
    ):
        response = client.post("/api/search/users", json={"query": "不存在zzz"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_users_blank_query_rejected(client):
    response = client.post("/api/search/users", json={"query": "   "})
    assert response.status_code == 400


def test_resolve_accepts_plain_uid_without_url_parse(client):
    extractor = _fake_extractor()
    with patch(
        "backend.app.routers.router_search._build_extractor",
        return_value=extractor,
    ):
        response = client.post("/api/search/resolve", json={"text": "20002"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uid"] == "20002"
    assert body["screen_name"] == "目标博主"
    extractor.resolve_url.assert_not_called()


def test_resolve_parses_share_text_url(client):
    extractor = _fake_extractor()
    with patch(
        "backend.app.routers.router_search._build_extractor",
        return_value=extractor,
    ):
        response = client.post(
            "/api/search/resolve",
            json={"text": "推荐博主 https://weibo.com/u/20002 快来看看"},
        )
    assert response.status_code == 200, response.text
    extractor.resolve_url.assert_called_once()


def test_resolve_rejects_plain_nickname(client):
    extractor = _fake_extractor()
    with patch(
        "backend.app.routers.router_search._build_extractor",
        return_value=extractor,
    ):
        response = client.post("/api/search/resolve", json={"text": "普通昵称"})
    assert response.status_code == 400
    assert "搜索" in response.json()["detail"]
