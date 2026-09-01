"""二维码会话 HTTP 路由契约。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.main import app


PNG = b"\x89PNG\r\n\x1a\nroute"
client = TestClient(app)


class FakeQRCodeService:
    def __init__(self, *, image: bytes | None = PNG) -> None:
        self.session_id = "12345678-1234-4234-9234-1234567890ab"
        self.bound_task_id: str | None = None
        self._image = image
        self.cancel_calls: list[str] = []

    def reserve(self):
        return SimpleNamespace(session_id=self.session_id)

    def bind_task(self, session_id: str, task_id: str) -> None:
        assert session_id == self.session_id
        self.bound_task_id = task_id

    def run(self, session_id: str) -> dict:
        assert session_id == self.session_id
        return {"logged_in": True, "cookie_source": "qrcode", "count": 1}

    def status(self, session_id: str) -> dict:
        assert session_id == self.session_id
        return {
            "session_id": session_id,
            "task_id": self.bound_task_id,
            "state": "waiting_scan",
            "message": "请使用微博 App 扫描二维码",
            "remaining_seconds": 119,
            "image_ready": self._image is not None,
            "result": None,
        }

    def image(self, session_id: str) -> bytes | None:
        assert session_id == self.session_id
        return self._image

    def task_id(self, session_id: str) -> str | None:
        assert session_id == self.session_id
        return self.bound_task_id

    def cancel(self, session_id: str) -> bool:
        assert session_id == self.session_id
        self.cancel_calls.append(session_id)
        return True

    def wait_closed(self, session_id: str, timeout: float) -> bool:
        assert session_id == self.session_id
        assert timeout == 35
        return True


def test_create_session_returns_task_and_random_session_identifiers() -> None:
    service = FakeQRCodeService()
    with patch(
        "backend.app.routers.router_login.qrcode_login_service", service
    ), patch("backend.app.routers.router_login.check_login"):
        response = client.post("/api/login/qrcode")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == service.session_id
    assert body["task_id"] == service.bound_task_id
    assert isinstance(body["task_id"], str) and body["task_id"]


def test_status_and_png_image_are_non_cacheable() -> None:
    service = FakeQRCodeService()
    with patch(
        "backend.app.routers.router_login.qrcode_login_service", service
    ):
        status = client.get(f"/api/login/qrcode/{service.session_id}/status")
        image = client.get(f"/api/login/qrcode/{service.session_id}/image")

    assert status.status_code == 200
    assert status.json()["state"] == "waiting_scan"
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["pragma"] == "no-cache"


def test_image_before_ready_returns_409_without_cache() -> None:
    service = FakeQRCodeService(image=None)
    with patch(
        "backend.app.routers.router_login.qrcode_login_service", service
    ):
        response = client.get(f"/api/login/qrcode/{service.session_id}/image")

    assert response.status_code == 409
    assert "二维码尚未就绪" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store"


def test_cancel_waits_until_browser_resources_are_closed() -> None:
    service = FakeQRCodeService()
    with patch(
        "backend.app.routers.router_login.qrcode_login_service", service
    ):
        response = client.post(f"/api/login/qrcode/{service.session_id}/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "closed": True}
    assert service.cancel_calls == [service.session_id]
