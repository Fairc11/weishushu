from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from weibo_book.archive.following import FollowingObjectRecord


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _identity():
    return {"uid": "10001", "screen_name": "本人"}


def _archive(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    root = tmp_path / "微博书"
    repository = ArchiveRepository.create(root, "10001", "本人")
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    repository.stage_following_items(snapshot_id, [
        FollowingObjectRecord(
            "blogger", "20001", "甲", "https://weibo.com/u/20001", "", 0
        ),
        FollowingObjectRecord(
            "supertopic", "1022:1", "超话甲", "//weibo.com/p/1",
            "sinaweibo://pageinfo?containerid=1", 0,
        ),
    ])
    repository.commit_following_snapshot(
        snapshot_id,
        cutoff_at="2026-07-18T01:00:00+00:00",
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=1,
        supertopic_reported_total=1,
    )
    repository.close()
    return root


def test_following_start_is_independent_and_forbids_unknown_fields(client, tmp_path):
    root = _archive(tmp_path)
    started = SimpleNamespace(
        task_id="0123456789ab", self_uid="10001", self_screen_name="本人"
    )
    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()), patch(
        "backend.app.routers.router_backup.FollowingArchiveTaskService.start",
        new=AsyncMock(return_value=started),
    ) as start:
        response = client.post("/api/following/start", json={
            "output_dir": str(root),
            "pacing_mode": "standard",
            "keep_awake_when_plugged": False,
        })
        invalid = client.post("/api/following/start", json={
            "output_dir": str(root),
            "pacing_mode": "standard",
            "keep_awake_when_plugged": False,
            "uid": "20002",
        })

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "0123456789ab",
        "mode": "update",
        "self_uid": "10001",
        "self_screen_name": "本人",
    }
    assert invalid.status_code == 422
    assert start.await_count == 1


def test_duration_check_is_local_only_and_returns_explicit_source(client, tmp_path):
    root = _archive(tmp_path)
    with patch(
        "backend.app.routers.router_backup.whoami",
        side_effect=AssertionError("本地时长检查不得执行身份网络请求"),
    ), patch(
        "weibo_book.archive.following_source.CrawlClientFollowingRequest.__call__"
    ) as network:
        response = client.post("/api/following/duration/check", json={
            "output_dir": str(root),
            "object_type": "blogger",
            "object_id": "20001",
        })

    assert response.status_code == 200, response.text
    assert response.json() == {
        "object_type": "blogger",
        "object_id": "20001",
        "source": "local_minimum",
        "platform_followed_at": "",
        "local_first_seen_at": "2026-07-18T01:00:00+00:00",
        "last_confirmed_at": "2026-07-18T01:00:00+00:00",
        "currently_following": True,
    }
    network.assert_not_called()


def test_duration_check_rejects_invalid_archive_and_missing_object(client, tmp_path):
    root = _archive(tmp_path)
    invalid = client.post("/api/following/duration/check", json={
        "output_dir": str(tmp_path / "不存在"),
        "object_type": "blogger",
        "object_id": "20001",
    })
    missing = client.post("/api/following/duration/check", json={
        "output_dir": str(root), "object_type": "blogger", "object_id": "99999",
    })

    assert invalid.status_code == 409
    assert missing.status_code == 404


def test_task_actions_dispatch_following_service(client, tmp_path):
    from backend.app.services.task_manager import task_manager

    async def create():
        return await task_manager.create_following_archive(
            output_dir=str((tmp_path / "微博书").resolve()),
            expected_uid="10001",
            snapshot_id="11111111-1111-4111-8111-111111111111",
        )

    import asyncio
    task_id = asyncio.run(create())
    with patch(
        "backend.app.routers.router_tasks.following_archive_tasks.pause",
        new=AsyncMock(return_value=True),
    ) as pause:
        response = client.post(f"/api/tasks/{task_id}/pause")

    assert response.status_code == 200
    assert response.json()["state"] == "pausing"
    pause.assert_awaited_once_with(task_id)


def test_resume_cancel_and_abandon_dispatch_following_service(client):
    task_id = "0123456789ab"
    task = {"task_id": task_id, "task_kind": "following_archive", "state": "waiting_resume"}
    with patch(
        "backend.app.routers.router_tasks.task_manager.snapshot",
        return_value=task,
    ), patch(
        "backend.app.routers.router_tasks.whoami",
        return_value=_identity(),
    ), patch(
        "backend.app.routers.router_tasks.following_archive_tasks.resume",
        new=AsyncMock(return_value=None),
    ) as resume, patch(
        "backend.app.routers.router_tasks.following_archive_tasks.cancel",
        new=AsyncMock(return_value=True),
    ) as cancel, patch(
        "backend.app.routers.router_tasks.following_archive_tasks.abandon",
        new=AsyncMock(return_value=True),
    ) as abandon:
        resumed = client.post(f"/api/tasks/{task_id}/resume")
        cancelled = client.post(f"/api/tasks/{task_id}/cancel")
        abandoned = client.post(f"/api/tasks/{task_id}/abandon")

    assert resumed.status_code == 200
    assert cancelled.json() == {"cancelled": True}
    assert abandoned.status_code == 200
    resume.assert_awaited_once_with(task_id, {"uid": "10001", "screen_name": "本人"})
    cancel.assert_awaited_once_with(task_id)
    abandon.assert_awaited_once_with(
        task_id, {"uid": "10001", "screen_name": "本人"}
    )
