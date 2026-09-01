"""扫码任务必须以服务端校验结果决定成功。"""

from unittest.mock import patch

import pytest

from backend.app.routers.router_login import _run_chrome_import, _run_qrcode_login
from weibo_book.errors import WeiboError, WeiboErrorKind


def test_qrcode_task_does_not_retry_or_accept_expired_disk_cookie():
    with patch("backend.app.routers.router_login.WeiboBook.ensure_login", return_value=None) as ensure, patch(
        "backend.app.routers.router_login.login_service.login_with_qrcode"
    ) as direct_login:
        with pytest.raises(WeiboError) as caught:
            _run_qrcode_login()

    assert caught.value.kind == WeiboErrorKind.AUTH
    assert "扫码登录失败或超时" in str(caught.value)
    ensure.assert_called_once_with(force=True)
    direct_login.assert_not_called()


def test_qrcode_task_requires_server_validated_cookie():
    with patch("backend.app.routers.router_login.WeiboBook.ensure_login", return_value="cookie-header"), patch(
        "backend.app.routers.router_login.load_cookies", return_value={"cookies": [{"name": "SUB", "value": "x"}]}
    ), patch("backend.app.routers.router_login.login_service.validate_stored_cookies", return_value=False):
        with pytest.raises(WeiboError) as caught:
            _run_qrcode_login()

    assert caught.value.kind == WeiboErrorKind.AUTH
    assert "未通过微博校验" in str(caught.value)


def test_qrcode_task_returns_validated_stored_cookie():
    stored = {"cookies": [{"name": "SUB", "value": "x"}]}
    with patch("backend.app.routers.router_login.WeiboBook.ensure_login", return_value="cookie-header"), patch(
        "backend.app.routers.router_login.load_cookies", return_value=stored
    ), patch("backend.app.routers.router_login.login_service.validate_stored_cookies", return_value=True):
        assert _run_qrcode_login() == stored


def test_chrome_import_returns_auth_error_when_no_cookie():
    with patch("weibo_book.chrome_import.import_from_chrome", return_value=None):
        with pytest.raises(WeiboError) as caught:
            _run_chrome_import()

    assert caught.value.kind == WeiboErrorKind.AUTH
    assert "Chrome 导入失败" in str(caught.value)


def test_chrome_import_returns_validated_count():
    with patch("weibo_book.chrome_import.import_from_chrome", return_value=[{"name": "SUB", "value": "x"}]):
        assert _run_chrome_import() == {"logged_in": True, "cookie_source": "chrome", "count": 1}
