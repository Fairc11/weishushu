"""桌面壳探针单元测试：不真实弹窗，全部 mock 路径。"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from webview.event import Event

import desktop.self_test.shell as shell


class _FakeWindow:
    def __init__(self, name: str, *, fail_destroy: bool = False, bridge_ready: bool = True) -> None:
        self.name = name
        self.events = type("Events", (), {"loaded": Event(None)})()
        self.cookie = ""
        self.evaluate_calls = []
        self.destroy_calls = 0
        self.fail_destroy = fail_destroy
        self.bridge_ready = bridge_ready

    def emit_loaded(self) -> None:
        self.events.loaded.set()

    def evaluate_js(self, script: str):
        self.evaluate_calls.append(script)
        if "getAttribute('data-weishushu-version')" in script:
            return "2.0.0" if self.bridge_ready else ""
        if "document.cookie='" in script:
            if "weishushu_self_test=ok" in script:
                self.cookie = "weishushu_self_test=ok"
            elif "weishushu_self_test=;" in script:
                self.cookie = ""
            return ""
        if "document.cookie" in script:
            return self.cookie
        return ""

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.fail_destroy:
            raise RuntimeError("destroy failed")


class _FakeWebview:
    def __init__(self, *, create_ok: bool = True, destroy_fail: bool = False, bridge_ready: bool = True) -> None:
        self.windows = []
        self.urls = []
        self.start_called = 0
        self.create_ok = create_ok
        self.destroy_fail = destroy_fail
        self.bridge_ready = bridge_ready

    def create_window(self, **kwargs):
        if not self.create_ok:
            raise RuntimeError("create_window failed")
        self.urls.append(kwargs["url"])
        window = _FakeWindow(f"window-{len(self.windows)}", fail_destroy=self.destroy_fail, bridge_ready=self.bridge_ready)
        self.windows.append(window)
        return window

    def start(self, func, **kwargs):
        self.start_called += 1
        for window in self.windows:
            window.emit_loaded()
        # 模拟 webview.start 不会把回调异常抛回主线程。
        func()


def _context():
    return SimpleNamespace(
        source_commit="abc",
        profile="user",
        platform="darwin",
    )


def test_connect_loaded_uses_real_pywebview_event(monkeypatch) -> None:
    loaded = Event(None)
    window = type("W", (), {"events": type("E", (), {"loaded": loaded})()})()
    flag = threading.Event()
    shell._connect_loaded(window, flag)
    loaded.set()
    assert flag.is_set()


def test_shell_smoke_success_path_and_loopback_urls(monkeypatch, tmp_path: Path) -> None:
    fake_webview = _FakeWebview()
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: False)
    monkeypatch.setattr(shell, "_load_webview", lambda: fake_webview)
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)

    assert output.exists()
    assert result["error_kind"] is None
    names = [item["name"] for item in result["steps"]]
    assert names == list(shell.SHELL_STEPS)
    assert all(item["status"] == "passed" for item in result["steps"])
    assert fake_webview.start_called == 1
    assert len(fake_webview.windows) == 2
    main, login = fake_webview.windows
    assert main.destroy_calls >= 1
    assert login.destroy_calls >= 1
    assert any("data-weishushu-version" in call for call in main.evaluate_calls)
    assert any("weishushu_self_test=ok" in call for call in login.evaluate_calls)
    assert any("weishushu_self_test=;" in call for call in login.evaluate_calls)

    # 两个窗口 URL 都必须通过回环 HTTP 服务提供。
    assert len(fake_webview.urls) == 2
    for url in fake_webview.urls:
        parsed = urlparse(url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"


def test_shell_smoke_bridge_dom_failure_returns_shell_error(monkeypatch, tmp_path: Path) -> None:
    fake_webview = _FakeWebview(bridge_ready=False)
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: False)
    monkeypatch.setattr(shell, "_load_webview", lambda: fake_webview)
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)
    assert result["error_kind"] == "shell"
    assert "pywebviewready/bridge 超时" in result["message"]


def test_shell_smoke_create_window_failure_returns_shell_error(monkeypatch, tmp_path: Path) -> None:
    fake_webview = _FakeWebview(create_ok=False)
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: False)
    monkeypatch.setattr(shell, "_load_webview", lambda: fake_webview)
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)
    assert result["error_kind"] == "shell"
    assert "create_window failed" in result["message"]


def test_shell_smoke_callback_timeout_does_not_escape_start(monkeypatch, tmp_path: Path) -> None:
    """工作线程/callback 异常不得由 webview.start 抛回主线程。"""
    fake_webview = _FakeWebview()
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: False)
    monkeypatch.setattr(shell, "_load_webview", lambda: fake_webview)
    monkeypatch.setattr(shell, "_wait_until", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timeout")))
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)
    assert result["error_kind"] == "shell"
    assert "timeout" in result["message"]


def test_shell_smoke_destroy_failure_returns_shell_error(monkeypatch, tmp_path: Path) -> None:
    fake_webview = _FakeWebview(destroy_fail=True)
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: False)
    monkeypatch.setattr(shell, "_load_webview", lambda: fake_webview)
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)
    assert result["error_kind"] == "shell"
    assert "窗口关闭失败" in result["message"]


def test_shell_smoke_environment_unavailable_returns_3_kind(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shell, "_desktop_session_unavailable", lambda: True)
    output = tmp_path / "shell.json"
    result = shell.run_shell_smoke(_context(), output)
    assert result["error_kind"] == "environment_unavailable"
    assert result["steps"][0]["status"] == "skipped"
    assert result["steps"][0]["skip_reason"] == "environment_unavailable"
