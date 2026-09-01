"""Windows Cookie ACL 收紧契约。

顺序铁律：先给当前账户显式完全控制，再移除继承；
任何一步失败都不得留下当前进程无法读取的 Cookie 文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from weibo_book import login as login_module
from weibo_book.errors import WeiboError


class FakeIcacls:
    """记录 icacls 调用并按脚本返回结果。"""

    def __init__(self, results: list[tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.results = list(results or [])

    def __call__(self, command, **_kwargs):
        assert command[0] == "icacls", command
        self.calls.append(command[2:])
        if self.results:
            returncode, out, err = self.results.pop(0)
        else:
            returncode, out, err = 0, "", ""
        return type("Completed", (), {"returncode": returncode, "stdout": out, "stderr": err})()


@pytest.fixture
def windows_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    return monkeypatch


def test_grant_runs_before_inheritance_and_checks_result(
    tmp_path, windows_platform, monkeypatch
):
    target = tmp_path / "cookies.json"
    target.write_text('{"cookies": []}', encoding="utf-8")
    fake = FakeIcacls()
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setattr(login_module, "_current_windows_account", lambda: "DESKTOP\\ASUS")

    login_module._restrict_file_permissions(target)

    assert fake.calls == [
        ["/grant", "DESKTOP\\ASUS:F"],
        ["/inheritance:r"],
    ]


def test_grant_failure_skips_inheritance_and_keeps_file_readable(
    tmp_path, windows_platform, monkeypatch
):
    target = tmp_path / "cookies.json"
    payload = '{"cookies": [{"name": "SUB"}]}'
    target.write_text(payload, encoding="utf-8")
    fake = FakeIcacls(results=[(1, "拒绝访问", "No mapping")])
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setattr(login_module, "_current_windows_account", lambda: "DESKTOP\\ASUS")

    login_module._restrict_file_permissions(target)

    assert len(fake.calls) == 1
    assert target.read_text(encoding="utf-8") == payload


def test_missing_principal_skips_acl_entirely(tmp_path, windows_platform, monkeypatch):
    target = tmp_path / "cookies.json"
    payload = '{"cookies": []}'
    target.write_text(payload, encoding="utf-8")
    fake = FakeIcacls()
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setattr(login_module, "_current_windows_account", lambda: None)

    login_module._restrict_file_permissions(target)

    assert fake.calls == []
    assert target.exists()


def test_unreadable_after_acl_triggers_repair_then_delete(
    tmp_path, windows_platform, monkeypatch
):
    target = tmp_path / "cookies.json"
    target.write_text('{"cookies": []}', encoding="utf-8")
    fake = FakeIcacls()
    monkeypatch.setattr("subprocess.run", fake)
    monkeypatch.setattr(login_module, "_current_windows_account", lambda: "D\\U")
    readability = {"ok": False}
    monkeypatch.setattr(
        login_module,
        "_file_readable_by_current_process",
        lambda _path: readability["ok"],
    )

    with pytest.raises(WeiboError) as excinfo:
        login_module._restrict_file_permissions(target)

    assert "重新登录" in excinfo.value.args[0]
    # 授权 + 移除继承 + 修复授权 = 三次调用
    assert fake.calls == [
        ["/grant", "D\\U:F"],
        ["/inheritance:r"],
        ["/grant", "D\\U:F"],
    ]
    assert not target.exists()


def test_current_windows_account_prefers_sam_and_falls_back_to_env(
    monkeypatch,
):
    class FakeWin32:
        NameSamCompatible = 3

        @staticmethod
        def GetUserNameEx(_style):
            return "DESKTOP-ABC\\asus"

    import types

    monkeypatch.setitem(sys.modules, "win32api", FakeWin32)
    assert login_module._current_windows_account() == "DESKTOP-ABC\\asus"

    monkeypatch.setitem(sys.modules, "win32api", None)
    monkeypatch.setenv("USERDOMAIN", "DESKTOP-ABC")
    monkeypatch.setenv("USERNAME", "asus")
    assert login_module._current_windows_account() == "DESKTOP-ABC\\asus"


def test_production_save_cookies_stays_readable_rewritable_deletable(tmp_path):
    """同进程最小回归：保存后立即可读、可覆盖、可删除。"""
    path = tmp_path / ".weibo_book_cookies_dev"
    cookies_round_1 = [
        {"name": "SUB", "value": "round-1", "domain": ".weibo.cn", "path": "/"}
    ]
    saved = login_module.save_cookies(cookies_round_1, str(path))
    assert saved[0]["value"] == "round-1"

    loaded = login_module.load_cookies(str(path))
    assert loaded["cookies"][0]["value"] == "round-1"
    assert path.read_text(encoding="utf-8").startswith("[")

    cookies_round_2 = [
        {"name": "SUB", "value": "round-2", "domain": ".weibo.cn", "path": "/"}
    ]
    login_module.save_cookies(cookies_round_2, str(path))
    assert "round-2" in path.read_text(encoding="utf-8")

    path.unlink()
    assert not path.exists()
