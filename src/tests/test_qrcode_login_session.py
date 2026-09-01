"""主界面二维码登录的进程级单会话契约。"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.qrcode_login import (
    QRCODE_SELECTOR,
    QRCodeLoginControl,
    QRCodeLoginEvent,
    QRCodeLoginService,
    _cookie_fingerprint,
)
from weibo_book.errors import OperationCancelled, WeiboError


PNG = b"\x89PNG\r\n\x1a\n" + b"qrcode"
COOKIES = [{"name": "SUB", "value": "secret", "domain": ".weibo.com"}]
STORED = [{"name": "SUB", "value": "saved", "domain": ".weibo.com"}]


def test_cookie_fingerprint_ignores_order_but_detects_value_change() -> None:
    baseline = [
        {"name": "SUB", "value": "visitor", "domain": ".weibo.com", "path": "/"},
        {"name": "WBPSESS", "value": "page", "domain": "weibo.com", "path": "/"},
    ]
    reordered = list(reversed(baseline))
    authenticated = [dict(item) for item in baseline]
    authenticated[0]["value"] = "account"

    assert _cookie_fingerprint(baseline) == _cookie_fingerprint(reordered)
    assert _cookie_fingerprint(baseline) != _cookie_fingerprint(authenticated)


def test_single_session_uses_uuid_and_rejects_overlapping_reservation() -> None:
    service = QRCodeLoginService(worker=lambda _control: iter(()))

    session = service.reserve()

    assert uuid.UUID(session.session_id).version == 4
    assert service.status(session.session_id)["state"] == "preparing"
    with pytest.raises(WeiboError, match="已有二维码登录会话"):
        service.reserve()


def test_login_validates_before_saving_and_clears_qrcode_at_terminal_state() -> None:
    calls: list[object] = []

    def worker(_control):
        yield QRCodeLoginEvent.qrcode(PNG)
        yield QRCodeLoginEvent.cookies(COOKIES)

    def validate(cookies):
        calls.append(("validate", cookies))
        return True

    def save(cookies):
        calls.append(("save", cookies))
        return STORED

    service = QRCodeLoginService(worker=worker, validator=validate, saver=save)
    session = service.reserve()
    service.bind_task(session.session_id, "task-1")

    result = service.run(session.session_id)

    assert calls == [("validate", COOKIES), ("save", COOKIES)]
    assert result == {"logged_in": True, "cookie_source": "qrcode", "count": 1}
    assert service.image(session.session_id) is None
    assert service.status(session.session_id) == {
        "session_id": session.session_id,
        "task_id": "task-1",
        "state": "authenticated",
        "message": "登录成功",
        "remaining_seconds": 0,
        "image_ready": False,
        "result": result,
    }
    assert service.wait_closed(session.session_id, timeout=0)


def test_unvalidated_cookie_is_never_saved() -> None:
    saved: list[object] = []

    def worker(_control):
        yield QRCodeLoginEvent.qrcode(PNG)
        yield QRCodeLoginEvent.cookies(COOKIES)
        yield QRCodeLoginEvent.cookies(COOKIES)

    validations = iter((False, True))
    service = QRCodeLoginService(
        worker=worker,
        validator=lambda _cookies: next(validations),
        saver=lambda cookies: saved.append(cookies) or STORED,
    )
    session = service.reserve()

    service.run(session.session_id)

    assert saved == [COOKIES]


def test_cancel_closes_worker_and_removes_qrcode_from_memory() -> None:
    worker_closed = threading.Event()
    image_ready = threading.Event()

    def worker(control):
        try:
            yield QRCodeLoginEvent.qrcode(PNG)
            image_ready.set()
            while not control.cancel_requested():
                yield QRCodeLoginEvent.tick()
                time.sleep(0.01)
        finally:
            worker_closed.set()

    service = QRCodeLoginService(worker=worker)
    session = service.reserve()
    errors: list[Exception] = []

    def run() -> None:
        try:
            service.run(session.session_id)
        except Exception as exc:  # 线程边界只保存类型供断言
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert image_ready.wait(1)

    assert service.cancel(session.session_id)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert worker_closed.is_set()
    assert len(errors) == 1 and isinstance(errors[0], OperationCancelled)
    status = service.status(session.session_id)
    assert status["state"] == "cancelled"
    assert status["image_ready"] is False
    assert service.image(session.session_id) is None
    assert service.wait_closed(session.session_id, timeout=0)


def test_expiry_is_terminal_and_does_not_save() -> None:
    now = [100.0]
    saved: list[object] = []

    def worker(_control):
        yield QRCodeLoginEvent.qrcode(PNG)
        now[0] = 103.0
        yield QRCodeLoginEvent.tick()

    service = QRCodeLoginService(
        worker=worker,
        validator=lambda _cookies: True,
        saver=lambda cookies: saved.append(cookies),
        timeout_seconds=2,
        monotonic=lambda: now[0],
    )
    session = service.reserve()

    with pytest.raises(WeiboError, match="二维码已过期"):
        service.run(session.session_id)

    assert saved == []
    assert service.status(session.session_id)["state"] == "expired"
    assert service.image(session.session_id) is None


@pytest.mark.parametrize(
    "payload",
    [b"", b"not-png", PNG + b"x" * (1024 * 1024)],
    # Windows 环境变量上限 32767 字符：pytest 会把参数 id 写进
    # PYTEST_CURRENT_TEST，大字节参数必须给短 id（pytest-dev/pytest#2951）。
    ids=["empty", "not-png", "oversized-1m"],
)
def test_invalid_qrcode_bytes_fail_closed(payload: bytes) -> None:
    def worker(_control):
        yield QRCodeLoginEvent.qrcode(payload)

    service = QRCodeLoginService(worker=worker)
    session = service.reserve()

    with pytest.raises(WeiboError, match="二维码图片无效"):
        service.run(session.session_id)

    assert service.status(session.session_id)["state"] == "error"
    assert service.image(session.session_id) is None


def test_playwright_worker_uses_verified_node_and_closes_all_resources() -> None:
    service = QRCodeLoginService()
    session = service.reserve()
    control = QRCodeLoginControl(session, time.monotonic)

    qrcode = MagicMock()
    qrcode.count.return_value = 1
    qrcode.bounding_box.return_value = {"width": 140, "height": 140}
    qrcode.screenshot.return_value = PNG
    heading = MagicMock()
    heading.count.return_value = 1

    login_page = MagicMock()
    login_page.locator.return_value = qrcode
    login_page.get_by_text.return_value = heading
    landing = MagicMock()
    landing.url = "https://weibo.com/newlogin"
    trigger = MagicMock()
    trigger.count.return_value = 1
    landing.get_by_role.return_value = trigger

    popup = MagicMock()
    popup.__enter__.return_value.value = login_page
    context = MagicMock()
    context.new_page.return_value = landing
    context.expect_page.return_value = popup
    browser = MagicMock()
    browser.new_context.return_value = context
    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=manager):
        events = service._playwright_events(control)
        first = next(events)
        session.cancel_event.set()
        assert list(events) == []

    assert first == QRCodeLoginEvent.qrcode(PNG)
    login_page.wait_for_selector.assert_called_once_with(
        QRCODE_SELECTOR,
        state="visible",
        timeout=15000,
    )
    playwright.chromium.launch.assert_called_once_with(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    landing.close.assert_called_once_with()
    login_page.close.assert_called_once_with()
    context.close.assert_called_once_with()
    browser.close.assert_called_once_with()


def test_playwright_worker_stops_after_cancelled_navigation() -> None:
    service = QRCodeLoginService()
    session = service.reserve()
    control = QRCodeLoginControl(session, time.monotonic)

    landing = MagicMock()

    def finish_navigation(*_args, **_kwargs):
        session.cancel_event.set()

    landing.goto.side_effect = finish_navigation
    context = MagicMock()
    context.new_page.return_value = landing
    browser = MagicMock()
    browser.new_context.return_value = context
    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    manager.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=manager):
        events = service._playwright_events(control)
        with pytest.raises(OperationCancelled):
            next(events)

    landing.wait_for_timeout.assert_not_called()
    landing.get_by_role.assert_not_called()
    landing.close.assert_called_once_with()
    context.close.assert_called_once_with()
    browser.close.assert_called_once_with()
