"""macOS 原生液态玻璃：启用条件与失败降级。"""
from __future__ import annotations

from desktop import native_glass


def test_macos_major_version_parsing():
    assert native_glass.macos_major_version("27.2.1") == 27
    assert native_glass.macos_major_version("15.7") == 15
    assert native_glass.macos_major_version("") == 0


def test_native_glass_only_on_macos_26_or_later():
    assert native_glass.native_glass_supported("win32", "27.0") is False
    assert native_glass.native_glass_supported("linux", "27.0") is False
    assert native_glass.native_glass_supported("darwin", "15.7") is False


def test_sidebar_geometry_matches_liquid_css():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[1] / "backend/app/static/css/liquid.css").read_text(encoding="utf-8")
    assert f"--lq-side-w: {int(native_glass.SIDEBAR_WIDTH)}px;" in css
    assert f"--lq-gap: {int(native_glass.SIDEBAR_INSET)}px;" in css
    assert f"border-radius: {int(native_glass.SIDEBAR_RADIUS)}px;" in css


class _Events:
    def __init__(self):
        self.shown = _Hook()
        self.loaded = _Hook()


class _Hook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _Window:
    uid = "missing-window"

    def __init__(self):
        self.events = _Events()
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


def test_page_is_not_marked_before_decoration_succeeds():
    window = _Window()
    native_glass.install_native_glass(window)
    # 页面加载先于原生玻璃接入成功时，不能通知网页改为透明
    for handler in window.events.loaded.handlers:
        handler()
    assert window.scripts == []


def test_set_window_appearance_rejects_unknown_theme():
    assert native_glass.set_window_appearance(_Window(), "sepia") is False
