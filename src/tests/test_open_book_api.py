"""书库“阅读”原生接口：只打开档案根目录下的 微博书.html。"""
from __future__ import annotations

from unittest.mock import patch

import js_api
from js_api import JsApi


def _popen_target(mock):
    return mock.call_args[0][0][-1]


def test_open_book_opens_only_the_book_file(tmp_path, monkeypatch):
    (tmp_path / "微博书.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(js_api.sys, "platform", "darwin")
    with patch.object(js_api.subprocess, "Popen") as popen:
        assert JsApi().open_book(str(tmp_path)) is True
    assert _popen_target(popen) == str(tmp_path / "微博书.html")


def test_open_book_rejects_missing_book(tmp_path):
    with patch.object(js_api.subprocess, "Popen") as popen:
        assert JsApi().open_book(str(tmp_path)) is False
    popen.assert_not_called()


def test_open_book_rejects_symlinked_book(tmp_path):
    target = tmp_path / "elsewhere.html"
    target.write_text("x", encoding="utf-8")
    (tmp_path / "微博书.html").symlink_to(target)
    with patch.object(js_api.subprocess, "Popen") as popen:
        assert JsApi().open_book(str(tmp_path)) is False
    popen.assert_not_called()


def test_open_release_page_opens_only_the_fixed_release_page():
    from backend.app.services.update_check import RELEASES_PAGE

    with patch("webbrowser.open", return_value=True) as opened:
        assert JsApi().open_release_page() is True
    opened.assert_called_once_with(RELEASES_PAGE)
