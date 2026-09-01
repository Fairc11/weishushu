"""阶段 4 仅开放本人归档，其余入口前后端同时禁用。"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.features import (
    CHROME_IMPORT_ENABLED,
    EMBEDDED_WEIBO_BROWSER_ENABLED,
    FUTURE_FEATURE_MESSAGE,
    PROFILE_ARCHIVE_ENABLED,
    SELF_ARCHIVE_ENABLED,
)
from backend.app.main import app


client = TestClient(app)


def test_stage4_feature_flags_are_fixed() -> None:
    assert SELF_ARCHIVE_ENABLED is True
    assert PROFILE_ARCHIVE_ENABLED is False
    assert EMBEDDED_WEIBO_BROWSER_ENABLED is False
    assert CHROME_IMPORT_ENABLED is False
    assert FUTURE_FEATURE_MESSAGE == "该功能正在开发中。"


def test_disabled_profile_routes_return_501_before_business_or_rate_limit() -> None:
    with patch(
        "backend.app.routers.router_profile.check_profile",
        side_effect=AssertionError("不得进入 profile 限流"),
    ), patch(
        "backend.app.routers.router_scraper.check_scraper",
        side_effect=AssertionError("不得进入 scraper 限流"),
    ), patch(
        "backend.app.routers.router_scraper.task_manager.create",
        side_effect=AssertionError("不得创建抓取任务"),
    ):
        responses = (
            client.post("/api/profile/resolve", json={}),
            client.post("/api/scrape/preview", json={"url": "https://weibo.com/u/1"}),
            client.post("/api/scrape/start", json={"url": "https://weibo.com/u/1"}),
        )

    for response in responses:
        assert response.status_code == 501
        assert response.json() == {"detail": FUTURE_FEATURE_MESSAGE}


def test_disabled_chrome_and_browser_routes_return_501_without_login_data() -> None:
    with patch(
        "backend.app.routers.router_login.check_login",
        side_effect=AssertionError("不得进入 Chrome 导入限流"),
    ), patch(
        "backend.app.routers.router_login._run_chrome_import",
        side_effect=AssertionError("不得读取 Chrome 登录数据"),
    ), patch(
        "backend.app.routers.router_browser._find_cookies_file",
        side_effect=AssertionError("不得读取内置浏览器登录数据"),
    ):
        chrome = client.post("/api/login/chrome", json={})
        browser = client.post("/api/browser/inject", json={})

    for response in (chrome, browser):
        assert response.status_code == 501
        assert response.json() == {"detail": FUTURE_FEATURE_MESSAGE}


def test_current_startup_does_not_create_mac_browser_controller() -> None:
    with patch(
        "desktop.browser.mac_webkit.MacWebKitBrowserController",
        side_effect=AssertionError("当前启动路径不得创建第二个 WKWebView"),
    ):
        from desktop_app import create_embedded_browser_controller

        assert create_embedded_browser_controller("darwin") is None


def test_browser_bridge_name_remains_but_does_not_open_window() -> None:
    from js_api import JsApi

    api = JsApi()
    with patch("webview.create_window") as create_window:
        result = api.open_browser_window()

    create_window.assert_not_called()
    assert result == {"ok": False, "error": FUTURE_FEATURE_MESSAGE}
