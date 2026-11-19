"""检查更新服务与端点测试（v2.1.0）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.main import app
from backend.app.services import update_check
from weibo_book.errors import WeiboError


def test_parse_version_strict():
    assert update_check.parse_version("v2.0.1") == (2, 0, 1)
    assert update_check.parse_version("2.1.0") == (2, 1, 0)
    assert update_check.parse_version("") is None
    assert update_check.parse_version("v2.0.1-beta") is None
    assert update_check.parse_version("2.x") is None


def test_is_newer_semver():
    assert update_check.is_newer("v2.1.0", "2.0.1") is True
    assert update_check.is_newer("v2.0.2", "2.0.1") is True
    assert update_check.is_newer("v2.0.1", "2.1.0") is False
    assert update_check.is_newer("v2.1.0", "2.1.0") is False
    assert update_check.is_newer("v2.1", "2.0.9") is True
    assert update_check.is_newer("不是版本", "2.1.0") is False


def _asset(name):
    return {"name": name, "browser_download_url": f"https://example.test/{name}"}


def test_pick_mac_dmg_and_sha256():
    assets = [
        _asset("Weishushu-v2.1.0-windows-x64.exe"),
        _asset("Weishushu-v2.1.0-macOS-arm64.dmg"),
        _asset("Weishushu-v2.1.0-macOS-arm64.dmg.sha256"),
    ]
    dmg = update_check.pick_mac_dmg(assets)
    assert dmg["name"] == "Weishushu-v2.1.0-macOS-arm64.dmg"
    assert update_check.sha256_asset_for(assets, dmg["name"])["name"].endswith(".sha256")
    assert update_check.pick_mac_dmg([_asset("Weishushu-v2.1.0-windows-x64.exe")]) is None
    assert update_check.sha256_asset_for([], "x.dmg") is None


def test_can_auto_install_false_in_source_run():
    assert update_check.can_auto_install() is False


def test_check_latest_release_parses_payload(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "tag_name": "v9.9.9",
                "body": "更新说明",
                "html_url": "https://example.test/release",
                "published_at": "2026-09-13T00:00:00Z",
                "assets": [_asset("Weishushu-v9.9.9-macOS-arm64.dmg")],
            }

    monkeypatch.setattr(update_check.httpx, "get", lambda *a, **kw: Response())
    info = update_check.check_latest_release()
    assert info["has_update"] is True
    assert info["latest"] == "v9.9.9"
    assert info["dmg_asset"] == "Weishushu-v9.9.9-macOS-arm64.dmg"
    assert info["can_auto_install"] is False


def test_check_latest_release_network_error_is_chinese(monkeypatch):
    def boom(*a, **kw):
        raise update_check.httpx.ConnectError("offline")

    monkeypatch.setattr(update_check.httpx, "get", boom)
    with pytest.raises(WeiboError, match="网络连接异常"):
        update_check.check_latest_release()


def test_check_latest_release_rejects_bad_payload(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"no_tag": True}

    monkeypatch.setattr(update_check.httpx, "get", lambda *a, **kw: Response())
    with pytest.raises(WeiboError, match="版本信息"):
        update_check.check_latest_release()


def test_apply_staged_update_rejects_invalid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "can_auto_install", lambda: True)
    bundle = tmp_path / "Weishushu.app"
    bundle.mkdir()
    monkeypatch.setattr(update_check, "bundle_path", lambda: bundle)
    with pytest.raises(WeiboError, match="暂存的更新无效"):
        update_check.apply_staged_update(str(tmp_path / "evil.app"))


def test_apply_staged_update_spawns_watcher_script(tmp_path, monkeypatch):
    monkeypatch.setattr(update_check, "can_auto_install", lambda: True)
    bundle = tmp_path / "Weishushu.app"
    bundle.mkdir()
    staged = tmp_path / "Weishushu-update-123.app"
    staged.mkdir()
    monkeypatch.setattr(update_check, "bundle_path", lambda: bundle)
    monkeypatch.setattr(update_check.platform_paths, "platform_paths", lambda: type("P", (), {"cache_dir": lambda self: tmp_path / "cache"})())
    spawned = {}

    class Process:
        pass

    def popen(command, **kwargs):
        spawned["command"] = command
        spawned["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(update_check.subprocess, "Popen", popen)
    result = update_check.apply_staged_update(str(staged))
    assert result["restarting"] is True
    script = Path(spawned["command"][1])
    text = script.read_text(encoding="utf-8")
    assert "Weishushu-update-123.app" in text
    assert ".Trash" in text
    assert "kill -0" in text
    assert spawned["kwargs"]["start_new_session"] is True


def test_update_routes(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(
        update_check, "check_latest_release",
        lambda: {"current": "2.1.0", "latest": "v2.1.0", "has_update": False,
                 "can_auto_install": False},
    )
    response = client.get("/api/update/check")
    assert response.status_code == 200
    assert response.json()["has_update"] is False

    def boom():
        raise WeiboError("检查更新失败：网络连接异常")

    monkeypatch.setattr(update_check, "check_latest_release", boom)
    response = client.get("/api/update/check")
    assert response.status_code == 502
    assert "网络连接异常" in response.json()["detail"]

    # 源码运行不是 frozen 正式版：安装与替换入口都必须拒绝
    response = client.post("/api/update/install")
    assert response.status_code == 400
    assert "正式安装版" in response.json()["detail"]
    response = client.post("/api/update/apply", json={"staged_path": "/tmp/x.app"})
    assert response.status_code == 400
