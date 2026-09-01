"""退出登录 HTTP 路由契约。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)

PATCH_TARGET = "weibo_book.login.get_cookie_file_path"


def test_logout_deletes_existing_cookie_file(tmp_path: Path) -> None:
    cookie_file = tmp_path / ".weibo_book_cookies"
    cookie_file.write_text('{"cookies": []}', encoding="utf-8")
    with patch(PATCH_TARGET, return_value=cookie_file):
        response = client.post("/api/login/logout")
    assert response.status_code == 200
    assert response.json() == {"logged_in": False, "cleared": True}
    assert not cookie_file.exists()


def test_logout_without_cookie_file_is_noop(tmp_path: Path) -> None:
    cookie_file = tmp_path / ".weibo_book_cookies"
    with patch(PATCH_TARGET, return_value=cookie_file):
        response = client.post("/api/login/logout")
    assert response.status_code == 200
    assert response.json() == {"logged_in": False, "cleared": False}


def test_logout_delete_failure_returns_chinese_500(tmp_path: Path) -> None:
    # 指向一个目录：exists() 为真，unlink() 抛 IsADirectoryError（OSError 子类）
    with patch(PATCH_TARGET, return_value=tmp_path):
        response = client.post("/api/login/logout")
    assert response.status_code == 500
    assert "退出登录失败" in response.json()["detail"]
