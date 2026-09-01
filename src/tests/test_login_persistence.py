"""Mac 内嵌登录信息跨进程保存契约。"""

import sys
from pathlib import Path

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from weibo_book import login as login_service
from weibo_book.login import load_cookies, save_cookies


def test_save_cookies_keeps_all_exact_browser_allowed_roots_and_attributes(tmp_path):
    path = tmp_path / "cookies.json"
    cookies = [
        {"name": "A", "value": "1", "domain": ".weibo.com", "path": "/", "expires": 1786550000, "secure": True, "httpOnly": True, "sameSite": "Lax"},
        {"name": "B", "value": "2", "domain": ".weibo.cn", "path": "/"},
        {"name": "C", "value": "3", "domain": ".sina.com.cn", "path": "/"},
        {"name": "D", "value": "4", "domain": ".sina.cn", "path": "/"},
        {"name": "E", "value": "5", "domain": ".weibo.com.evil.example", "path": "/"},
    ]

    saved = save_cookies(cookies, str(path))
    loaded = load_cookies(str(path))

    assert [item["domain"] for item in saved] == [".weibo.com", ".weibo.cn", ".sina.com.cn", ".sina.cn"]
    assert loaded["cookies"][0]["expires"] == 1786550000
    assert loaded["cookies"][0]["secure"] is True
    assert loaded["cookies"][0]["httpOnly"] is True
    assert loaded["cookies"][0]["sameSite"] == "Lax"


def test_save_cookies_keeps_same_name_and_domain_on_distinct_paths(tmp_path):
    path = tmp_path / "cookies.json"
    cookies = [
        {"name": "TOKEN", "value": "root", "domain": ".weibo.cn", "path": "/"},
        {"name": "TOKEN", "value": "api", "domain": ".weibo.cn", "path": "/api"},
    ]

    saved = save_cookies(cookies, str(path))

    assert [(item["name"], item["domain"], item["path"]) for item in saved] == [
        ("TOKEN", ".weibo.cn", "/"),
        ("TOKEN", ".weibo.cn", "/api"),
    ]


def test_save_cookies_permission_failure_preserves_previous_login(
    tmp_path, monkeypatch
):
    path = tmp_path / "cookies.json"
    path.write_text('[{"name":"SUB","value":"old"}]', encoding="utf-8")

    def fail_permissions(_path):
        raise OSError("模拟权限收紧失败")

    monkeypatch.setattr(login_service, "_restrict_file_permissions", fail_permissions)

    with pytest.raises(OSError, match="权限收紧失败"):
        save_cookies(
            [{"name": "SUB", "value": "new", "domain": ".weibo.cn", "path": "/"}],
            str(path),
        )

    assert path.read_text(encoding="utf-8") == '[{"name":"SUB","value":"old"}]'
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != "darwin", reason="NSHTTPCookie 仅 macOS 提供")
def test_saved_cookie_record_can_be_recreated_as_nshttpcookie():
    from desktop.browser.mac_webkit import cookie_record_to_ns_cookie

    restored = cookie_record_to_ns_cookie({
        "name": "SUB",
        "value": "opaque-value",
        "domain": ".weibo.cn",
        "path": "/",
        "expires": 1786550000,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    })

    assert str(restored.name()) == "SUB"
    assert str(restored.domain()) == ".weibo.cn"
    assert restored.isSecure() is True
    assert restored.isHTTPOnly() is True
    assert int(restored.expiresDate().timeIntervalSince1970()) == 1786550000


@pytest.mark.skipif(sys.platform != "darwin", reason="仅 macOS 提供 WebKit")
def test_controller_accepts_cookie_source_for_restore():
    from desktop.browser.mac_webkit import MacWebKitBrowserController

    source = lambda: [{"name": "SUB", "value": "opaque", "domain": ".weibo.cn", "path": "/"}]
    controller = MacWebKitBrowserController(cookie_sink=lambda _cookies: True, cookie_source=source)

    assert controller.cookie_source is source


def test_cookie_header_only_contains_domains_matching_target_host():
    records = [
        {"name": "ROOT", "value": "1", "domain": ".weibo.cn", "path": "/"},
        {"name": "HOST", "value": "2", "domain": "m.weibo.cn", "path": "/"},
        {"name": "OTHER", "value": "3", "domain": ".weibo.com", "path": "/"},
        {"name": "EVIL", "value": "4", "domain": ".weibo.cn.evil.example", "path": "/"},
    ]

    assert login_service.cookies_to_header_for_host(records, "m.weibo.cn") == "ROOT=1; HOST=2"


@pytest.mark.skipif(sys.platform != "darwin", reason="仅 macOS 提供 WebKit")
def test_finished_navigation_triggers_automatic_cookie_sync():
    from desktop.browser.mac_webkit import _NavigationDelegate

    controller = MagicMock()
    delegate = _NavigationDelegate.alloc().initWithController_(controller)

    _NavigationDelegate.webView_didFinishNavigation_(delegate, MagicMock(), None)

    controller._navigation_finished.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "darwin", reason="仅 macOS 提供 WebKit")
def test_user_profile_uses_default_webkit_data_store():
    from desktop.browser import mac_webkit

    default_store = object()
    default_data_store = MagicMock(return_value=default_store)
    non_persistent_data_store = MagicMock()
    fake_webkit = SimpleNamespace(
        WKWebsiteDataStore=SimpleNamespace(
            defaultDataStore=default_data_store,
            nonPersistentDataStore=non_persistent_data_store,
        )
    )
    with patch("backend.app.profile.is_dev_profile", return_value=False), \
         patch.object(mac_webkit, "WebKit", fake_webkit):
        assert mac_webkit.website_data_store_for_profile() is default_store

    default_data_store.assert_called_once_with()
    non_persistent_data_store.assert_not_called()


@pytest.mark.skipif(sys.platform != "darwin", reason="仅 macOS 提供 WebKit")
def test_dev_profile_uses_non_persistent_webkit_data_store():
    from desktop.browser import mac_webkit

    dev_store = object()
    default_data_store = MagicMock()
    non_persistent_data_store = MagicMock(return_value=dev_store)
    fake_webkit = SimpleNamespace(
        WKWebsiteDataStore=SimpleNamespace(
            defaultDataStore=default_data_store,
            nonPersistentDataStore=non_persistent_data_store,
        )
    )
    with patch("backend.app.profile.is_dev_profile", return_value=True), \
         patch.object(mac_webkit, "WebKit", fake_webkit):
        assert mac_webkit.website_data_store_for_profile() is dev_store

    non_persistent_data_store.assert_called_once_with()
    default_data_store.assert_not_called()
