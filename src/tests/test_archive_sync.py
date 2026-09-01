from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import time
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.discovery import ProfileItem, ProfilePage
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import CommentRecord, MediaRecord, PostRecord
from weibo_book.errors import OperationCancelled, WeiboError, WeiboErrorKind
from weibo_book.models import Comment, LinkCard, MediaType, Post, PostMedia


def test_sync_result_exposes_explicit_total_posts():
    from weibo_book.archive.sync import SyncResult

    result = SyncResult("create", 137, 0, 0, 0, [], total_posts=137)

    assert result.total_posts == 137
    assert result.new_posts == 137
    assert result.refreshed_posts == 0
    assert result.changed_posts == 0


class IdentityProvider:
    def __init__(self, uid: str = "10001", screen_name: str = "测试用户") -> None:
        self.uid = uid
        self.screen_name = screen_name
        self.calls = 0

    def whoami(self) -> dict:
        self.calls += 1
        return {"uid": self.uid, "screen_name": self.screen_name}


class FakeSource:
    def __init__(self, pages: list[ProfilePage], posts: dict[str, Post]) -> None:
        self.pages = pages
        self.posts = posts
        self.fetches: list[str] = []
        self.comment_fetches: list[tuple[str, int]] = []
        self.failures: dict[str, Exception] = {}
        self.profile_iterations = 0

    def iter_profile_pages(self, uid: str):
        assert uid == "10001"
        self.profile_iterations += 1
        yield from self.pages

    def fetch_post(self, uid: str, bid: str) -> Post:
        assert uid == "10001"
        self.fetches.append(bid)
        failure = self.failures.get(bid)
        if failure is not None:
            raise failure
        return self.posts[bid]

    def fetch_recent_comments(self, post_id: str, limit: int = 10) -> list[Comment]:
        self.comment_fetches.append((post_id, limit))
        return [
            Comment(
                id=f"comment-{post_id}",
                text="评论正文",
                user_name="评论者",
                user_id="20002",
                user_avatar="avatar.jpg",
                created_at="2026-07-14 01:00:00",
                like_counts=2,
                source="来自 iPhone",
                image_url="https://example.test/comment.jpg",
                parent_id=None,
            )
        ]


def page(*bids: str, is_last: bool = True) -> ProfilePage:
    return ProfilePage([ProfileItem(bid) for bid in bids], is_last=is_last)


def post(bid: str, *, text: str = "正文", likes: int = 0) -> Post:
    return Post(
        bid=bid,
        uid="10001",
        user_name="测试用户",
        user_avatar="avatar.jpg",
        text=text,
        created_at=datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc),
        source="iPhone 客户端",
        ip_location="发布于 测试地区",
        is_pinned=True,
        pin_order=1,
        reposts_count=1,
        comments_count=2,
        likes_count=likes,
        media=[
            PostMedia(
                type=MediaType.IMAGE,
                url="https://example.test/original.jpg",
                thumbnail="https://example.test/thumb.jpg",
                local_path="media/posts/original.jpg",
                local_thumb="media/posts/thumb.jpg",
                width=100,
                height=80,
            )
        ],
        link_card=LinkCard(
            type="video",
            title="链接标题",
            description="链接描述",
            image_url="https://example.test/card.jpg",
            url="https://example.test/card",
        ),
    )


def seed_archive(root: Path, *records: PostRecord) -> None:
    repo = ArchiveRepository.create(root, "10001", "测试用户")
    for record in records:
        repo.upsert_post(record)
        repo.replace_current_comments(
            record.bid,
            [CommentRecord(f"old-comment-{record.bid}", record.bid, None, {"text": "旧评论"}, "old")],
        )
    repo.close()


def record(bid: str, *, text: str = "旧正文", visibility: str = "visible") -> PostRecord:
    return PostRecord(bid=bid, uid="10001", text=text, visibility=visibility)


def database_hash(root: Path) -> str:
    return hashlib.sha256((root / "data" / "archive.db").read_bytes()).hexdigest()


def test_legacy_index_is_not_converted_and_is_cleaned_after_next_success(tmp_path):
    from backend.app.services.backup_index import (
        cleanup_legacy_audit,
        finalize_legacy_archive,
        stage_legacy_archive,
        staged_legacy_archive_sha256,
    )
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    root.mkdir()
    legacy_payload = '{"uid":"10001","bids":["OLD"]}'
    (root / ".weishushu_index.json").write_text(legacy_payload, encoding="utf-8")
    task_id = "0123456789ab"
    staged = stage_legacy_archive(root, "10001", task_id)

    PersonalArchiveSync(
        root,
        FakeSource([page("NEW")], {"NEW": post("NEW")}),
        IdentityProvider(),
    ).run("create")
    completed = ArchiveRepository.open(root, "10001")
    run_id, status = completed.get_latest_sync_status("create")
    completed.close()
    assert status == "done"
    index_sha256 = staged_legacy_archive_sha256(root, task_id)
    finalize_legacy_archive(
        root, staged, task_id, "10001", run_id, "create", index_sha256
    )

    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_post("NEW") is not None
    assert repository.get_post("OLD") is None
    repository.close()
    audit = root / ".work" / "legacy" / ".weishushu_index.json"
    assert audit.read_text(encoding="utf-8") == legacy_payload

    PersonalArchiveSync(
        root,
        FakeSource([page("NEW")], {"NEW": post("NEW")}),
        IdentityProvider(),
    ).run("incremental")
    cleanup_legacy_audit(root)

    assert not audit.exists()


def test_sync_emits_monotonic_real_progress_totals(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    events = []
    source = FakeSource([page("A", "B")], {"A": post("A"), "B": post("B")})
    PersonalArchiveSync(
        tmp_path / "archive",
        source,
        IdentityProvider(),
        media_stager=FakeMediaStager(),
        progress_callback=events.append,
    ).run("create")

    phases = [event["phase"] for event in events]
    for phase in ("identify", "discover", "extract", "comments", "media", "generate", "complete"):
        assert phase in phases
    unknown_discovery = next(
        event for event in events
        if event["phase"] == "discover" and event["total"] is None
    )
    assert unknown_discovery["current"] == 1
    planned = next(
        event for event in events
        if event["phase"] == "discover" and event["total"] == 2
    )
    assert planned["current"] == 0
    for phase in ("extract", "comments", "media"):
        phase_events = [event for event in events if event["phase"] == phase]
        assert [event["current"] for event in phase_events] == [1, 2]
        assert all(event["total"] == 2 for event in phase_events)
    generate_events = [event for event in events if event["phase"] == "generate"]
    assert [event["current"] for event in generate_events] == [0, 1]
    assert all(event["total"] == 1 for event in generate_events)
    assert [event["pct"] for event in events] == sorted(event["pct"] for event in events)


def test_empty_sync_still_emits_zero_total_processing_phases(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    events = []
    PersonalArchiveSync(
        tmp_path / "archive",
        FakeSource([page()], {}),
        IdentityProvider(),
        progress_callback=events.append,
    ).run("create")

    for phase in ("extract", "comments", "media"):
        event = next(value for value in events if value["phase"] == phase)
        assert event["current"] == 0
        assert event["total"] == 0


def test_homepage_pin_metadata_overrides_detail_and_can_unpin(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pinned_page = ProfilePage([ProfileItem("A", True, 1)], is_last=True)
    source = FakeSource([pinned_page], {"A": replace(post("A"), is_pinned=False, pin_order=None)})
    PersonalArchiveSync(root, source, IdentityProvider()).run("create")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A").is_pinned is True
    assert repo.get_post("A").pin_order == 1
    repo.close()

    source = FakeSource([ProfilePage([ProfileItem("A", False, None)], is_last=True)], {"A": post("A")})
    result = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A").is_pinned is False
    assert repo.get_post("A").pin_order is None
    assert result.changed_posts == 1
    repo.close()


def test_create_maps_complete_post_and_comment_fields(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    source = FakeSource([page("A")], {"A": post("A")})
    result = PersonalArchiveSync(root, source, IdentityProvider()).run("create")

    assert result.mode == "create"
    assert result.new_posts == 1
    assert source.fetches == ["A"]
    assert source.comment_fetches == [("A", 10)]
    repo = ArchiveRepository.open(root, "10001")
    stored = repo.get_post("A")
    assert stored == PostRecord(
        bid="A",
        uid="10001",
        text="正文",
        created_at="2026-07-14T01:02:03+00:00",
        source="iPhone 客户端",
        ip_location="发布于 测试地区",
        is_pinned=False,
        pin_order=None,
        reposts_count=1,
        comments_count=2,
        likes_count=0,
        link_card_payload={
            "description": "链接描述",
            "image_url": "https://example.test/card.jpg",
            "local_image": None,
            "original_url": "",
            "title": "链接标题",
            "type": "video",
            "url": "https://example.test/card",
        },
        media_signature=[{
            "duration": None,
            "height": 80,
            "local_path": "media/posts/original.jpg",
            "local_thumb": "media/posts/thumb.jpg",
            "position": 0,
            "thumbnail": "https://example.test/thumb.jpg",
            "type": "image",
            "url": "https://example.test/original.jpg",
            "video_cover": None,
            "width": 100,
        }],
    )
    row = repo._connection.execute(
        "SELECT payload_json FROM comments WHERE post_bid = 'A'"
    ).fetchone()
    assert '"图片"' not in row[0]
    assert '"image_url": "https://example.test/comment.jpg"' in row[0]
    assert repo.manifest().last_successful_sync_at
    repo.close()


def test_incremental_fetches_all_new_and_only_five_known_without_duplicates(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    old = [f"old-{index}" for index in range(60)]
    seed_archive(root, *(record(bid) for bid in old))
    new = [f"new-{index}" for index in range(81)]
    ordered = new + old + [new[0], old[0]]
    source = FakeSource(
        [page(*ordered)],
        {bid: post(bid, text=f"updated-{bid}") for bid in new + old},
    )

    result = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")

    assert result.new_posts == 81
    assert result.refreshed_posts == 5
    assert len(source.fetches) == 86
    assert len(set(source.fetches)) == 86
    assert source.fetches == new + old[:5]


def test_not_found_marks_existing_unavailable_but_network_error_propagates(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root, record("old"))
    source = FakeSource([page("old")], {"old": post("old")})
    source.failures["old"] = WeiboError("已不可见", kind=WeiboErrorKind.NOT_FOUND)

    result = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert result.unavailable_posts == 1
    assert repo.get_post("old") == replace(record("old"), visibility="unavailable")
    assert repo._connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
    repo.close()

    source.failures["old"] = WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)
    with pytest.raises(WeiboError) as error:
        PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert error.value.kind is WeiboErrorKind.NETWORK
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("old").visibility == "unavailable"
    repo.close()


def test_mismatched_detail_is_skipped_without_aborting_create(tmp_path):
    """时间轴混入广告/推广卡导致详情 UID 或 BID 不一致时，跳过该条不中断整轮备份。

    2026-09-01 真实备份在 539/1202 处整轮崩溃的实锤回归。
    """
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    good = [f"good-{index}" for index in range(3)]
    posts = {bid: post(bid) for bid in good}
    posts["ad-card"] = replace(post("ad-card"), uid="99999")
    posts["bid-drift"] = replace(post("bid-drift"), bid="bid-other")
    source = FakeSource(
        [page("good-0", "ad-card", "good-1", "bid-drift", "good-2")],
        posts,
    )

    result = PersonalArchiveSync(root, source, IdentityProvider()).run("create")

    assert result.new_posts == 3
    assert result.unavailable_posts == 2
    repo = ArchiveRepository.open(root, "10001")
    try:
        assert repo.get_post("ad-card") is None
        assert repo.get_post("bid-drift") is None
        for bid in good:
            assert repo.get_post(bid) is not None
    finally:
        repo.close()


def test_create_failure_does_not_leave_formal_archive_and_preserves_empty_directory(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    for existing in (False, True):
        root = tmp_path / ("existing" if existing else "missing")
        if existing:
            root.mkdir()
        source = FakeSource([page("A")], {"A": post("A")})
        source.failures["A"] = WeiboError("断网", kind=WeiboErrorKind.NETWORK)
        with pytest.raises(WeiboError):
            PersonalArchiveSync(root, source, IdentityProvider()).run("create")
        assert root.exists() is existing
        if existing:
            assert list(root.iterdir()) == []


def test_create_crash_keeps_task_temporary_archive_and_resumes_without_refetch(tmp_path):
    """意外中断（非主动暂停）不再删除任务临时归档，续跑不重抓已提交条目。

    2026-09-01 真实备份 539/1202 崩溃后进度全丢的实锤回归。
    """
    from weibo_book.archive.sync import PersonalArchiveSync

    task_id = "0123456789ab"
    root = tmp_path / "微博书"
    temporary = tmp_path / f".微博书.create-task-{task_id}"
    source = FakeSource([page("A", "B")], {"A": post("A"), "B": post("B")})
    source.failures["B"] = WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(WeiboError) as error:
        PersonalArchiveSync(
            root, source, IdentityProvider(), task_id=task_id
        ).run("create")
    assert error.value.kind is WeiboErrorKind.NETWORK

    # 崩溃保留临时归档与同步恢复点，正式目录仍未创建
    assert temporary.is_dir()
    assert not root.exists()
    crashed = ArchiveRepository.open(temporary, "10001")
    try:
        assert crashed.get_post("A") is not None
        unfinished = crashed.get_unfinished_sync("create")
        assert unfinished is not None
        assert "A" in unfinished.checkpoint["completed_bids"]
    finally:
        crashed.close()

    source.failures.clear()
    source.fetches.clear()
    result = PersonalArchiveSync(
        root, source, IdentityProvider(), task_id=task_id
    ).run("create")

    assert result.new_posts == 2
    assert source.fetches == ["B"]
    assert not temporary.exists()
    archive = ArchiveRepository.open(root, "10001")
    try:
        assert archive.get_post("A") is not None
        assert archive.get_post("B") is not None
    finally:
        archive.close()


def test_uid_mismatch_and_invalid_mode_are_rejected_after_whoami(tmp_path):
    from weibo_book.archive.repository import ArchiveIdentityError
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root, record("old"))
    identity = IdentityProvider(uid="20002", screen_name="其他用户")
    source = FakeSource([], {})
    with pytest.raises(ArchiveIdentityError, match="其他账号"):
        PersonalArchiveSync(root, source, identity).run("incremental")
    assert identity.calls == 1

    identity = IdentityProvider()
    with pytest.raises(WeiboError, match="同步模式"):
        PersonalArchiveSync(root, source, identity).run("invalid")
    assert identity.calls == 1


def test_rebuild_failure_keeps_database_hash_and_mtime_then_success_replaces(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root, record("old"))
    db = root / "data" / "archive.db"
    before = (database_hash(root), db.stat().st_mtime_ns)
    source = FakeSource([page("new")], {"new": post("new")})
    source.failures["new"] = WeiboError("断网", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(WeiboError):
        PersonalArchiveSync(root, source, IdentityProvider()).run("rebuild")
    assert (database_hash(root), db.stat().st_mtime_ns) == before

    source.failures.clear()
    result = PersonalArchiveSync(root, source, IdentityProvider()).run("rebuild")
    assert result.new_posts == 1
    repo = ArchiveRepository.open(root, "10001")
    assert repo.list_known_bids() == {"new"}
    repo.close()


def test_incremental_resume_after_twenty_commits_fetches_only_remaining(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    bids = [f"new-{index:02d}" for index in range(25)]
    source = FakeSource([page(*bids)], {bid: post(bid) for bid in bids})
    source.failures[bids[20]] = WeiboError("断网", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(WeiboError):
        PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.fetches == bids[:21]

    source.fetches.clear()
    source.failures.clear()
    class PacingRecorder:
        def __init__(self):
            self.posts = []

        def set_known_remaining(self, *, posts):
            self.posts.append(posts)

    pacing = PacingRecorder()
    result = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        pacing_scheduler=pacing,
    ).run("incremental")
    assert source.fetches == bids[20:]
    assert pacing.posts == [5]
    assert result.new_posts == 25
    repo = ArchiveRepository.open(root, "10001")
    assert repo.list_known_bids() == set(bids)
    assert repo._connection.execute("SELECT COUNT(*) FROM post_revisions").fetchone()[0] == 0
    assert repo._connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 25
    assert repo._connection.execute(
        "SELECT COUNT(*) FROM sync_runs WHERE status = 'done'"
    ).fetchone()[0] == 1
    repo.close()

    source.fetches.clear()
    third = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.fetches == bids[:5]
    assert third.new_posts == 0
    assert third.refreshed_posts == 5


def test_resume_uses_saved_pending_classification_without_rediscovery(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    bids = [f"new-{index:02d}" for index in range(80)]
    pages = [
        page(*bids[index:index + 10], is_last=index == 70)
        for index in range(0, 80, 10)
    ]
    source = FakeSource(pages, {bid: post(bid) for bid in bids})
    source.failures[bids[60]] = WeiboError("断网", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(WeiboError):
        PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.profile_iterations == 1
    assert source.fetches == bids[:61]

    source.fetches.clear()
    source.failures.clear()
    result = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.profile_iterations == 1
    assert source.fetches == bids[60:]
    assert result.new_posts == 80

    repo = ArchiveRepository.open(root, "10001")
    assert repo.list_known_bids() == set(bids)
    repo.close()

    source.fetches.clear()
    third = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.profile_iterations == 2
    assert third.new_posts == 0


def test_corrupt_pending_checkpoint_is_rejected_in_chinese(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    repo = ArchiveRepository.open(root, "10001")
    run_id = repo.begin_sync("incremental")
    repo.update_sync_checkpoint(
        run_id,
        {
            "pending_bids": ["A", "A"],
            "completed_bids": ["A"],
            "new_bids": ["A"],
            "refresh_bids": [],
            "counters": {},
        },
    )
    repo.finish_sync(run_id, "error", {})
    repo.close()

    with pytest.raises(WeiboError, match="恢复点"):
        PersonalArchiveSync(
            root, FakeSource([], {}), IdentityProvider()
        ).run("incremental")


def test_empty_profile_checkpoint_resumes_without_rediscovery(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    repo = ArchiveRepository.open(root, "10001")
    run_id = repo.begin_sync("incremental")
    repo.update_sync_checkpoint(
        run_id,
        {
            "pending_bids": [],
            "completed_bids": [],
            "new_bids": [],
            "refresh_bids": [],
            "counters": {
                "new_posts": 0,
                "refreshed_posts": 0,
                "changed_posts": 0,
                "unavailable_posts": 0,
            },
        },
    )
    repo.finish_sync(run_id, "error", {"error": "模拟生成阶段失败"})
    repo.close()
    source = FakeSource([page("must-not-discover")], {})

    result = PersonalArchiveSync(
        root, source, IdentityProvider()
    ).run("incremental")

    assert source.profile_iterations == 0
    assert result.new_posts == 0
    assert result.refreshed_posts == 0
    assert result.changed_posts == 0
    assert result.unavailable_posts == 0
    repo = ArchiveRepository.open(root, "10001")
    assert repo.manifest().last_successful_sync_at
    assert repo._connection.execute(
        "SELECT status FROM sync_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0] == "done"
    repo.close()


def test_post_data_and_completed_checkpoint_rollback_together(tmp_path, monkeypatch):
    from weibo_book.archive import repository as repository_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    source = FakeSource([page("A")], {"A": post("A")})
    original = repository_module.ArchiveRepository.update_sync_checkpoint
    failed = False

    def fail_after_data(self, run_id, checkpoint):
        nonlocal failed
        if (
            not failed
            and self._connection.in_transaction
            and checkpoint.get("completed_bids") == ["A"]
        ):
            failed = True
            raise WeiboError("模拟 checkpoint 写入失败")
        return original(self, run_id, checkpoint)

    monkeypatch.setattr(
        repository_module.ArchiveRepository,
        "update_sync_checkpoint",
        fail_after_data,
    )
    with pytest.raises(WeiboError, match="checkpoint"):
        PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")

    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    assert repo._connection.execute(
        "SELECT COUNT(*) FROM comments WHERE post_bid = 'A'"
    ).fetchone()[0] == 0
    repo.close()

    source.fetches.clear()
    result = PersonalArchiveSync(root, source, IdentityProvider()).run("incremental")
    assert source.fetches == ["A"]
    assert result.new_posts == 1


def test_cancellation_marks_run_cancelled_without_finishing_remaining_posts(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    source = FakeSource([page("A", "B")], {"A": post("A"), "B": post("B")})
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(OperationCancelled, match="任务已取消"):
        PersonalArchiveSync(
            root, source, IdentityProvider(), cancel_requested=cancelled
        ).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert repo._connection.execute(
        "SELECT status FROM sync_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0] == "cancelled"
    repo.close()


@pytest.mark.parametrize("stage", ["fetch", "comments", "stager", "precommit"])
def test_cancellation_checks_each_post_io_stage(tmp_path, stage):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    state = {"cancel": False, "armed_calls": 0}

    class CancellingSource(FakeSource):
        def fetch_post(self, uid, bid):
            value = super().fetch_post(uid, bid)
            if stage == "fetch":
                state["cancel"] = True
            return value

        def fetch_recent_comments(self, post_id, limit=10):
            value = super().fetch_recent_comments(post_id, limit)
            if stage == "comments":
                state["cancel"] = True
            return value

    class CancellingStager(FakeMediaStager):
        def stage(self, *args, cancel_requested=None):
            value = super().stage(*args, cancel_requested=cancel_requested)
            if stage in {"stager", "precommit"}:
                state["cancel"] = True
            assert cancel_requested is not None
            return value

    def cancelled():
        if not state["cancel"]:
            return False
        state["armed_calls"] += 1
        return stage != "precommit" or state["armed_calls"] >= 2

    source = CancellingSource([page("A")], {"A": post("A")})
    with pytest.raises(OperationCancelled, match="任务已取消"):
        PersonalArchiveSync(
            root,
            source,
            IdentityProvider(),
            media_stager=CancellingStager(),
            cancel_requested=cancelled,
        ).run("incremental")

    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    assert repo._connection.execute(
        "SELECT status FROM sync_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0] == "cancelled"
    repo.close()


def test_cancellation_before_finish_keeps_manifest_success_time_unchanged(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 4

    with pytest.raises(OperationCancelled):
        PersonalArchiveSync(
            root,
            FakeSource([page()], {}),
            IdentityProvider(),
            cancel_requested=cancelled,
        ).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.manifest().last_successful_sync_at == ""
    assert repo._connection.execute(
        "SELECT status FROM sync_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0] == "cancelled"
    repo.close()


class FakeMediaStager:
    def stage(
        self,
        post_value: Post,
        comments: list[Comment],
        work_root: Path,
        cancel_requested=None,
    ):
        from weibo_book.archive.sync import StagedMedia

        staged = work_root / f"{post_value.bid}.jpg"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(post_value.bid.encode())
        return [
            StagedMedia(
                MediaRecord(
                    "post", post_value.bid, "image", 0,
                    "https://example.test/image.jpg", f"media/{post_value.bid}.jpg"
                ),
                staged,
            )
        ]


def test_comment_payload_excludes_nested_replies_and_keeps_parent_rows(tmp_path):
    from weibo_book.archive.sync import comments_to_records

    reply = Comment(
        id="reply",
        text="回复",
        user_name="回复者",
        user_id="3",
        user_avatar="",
        created_at="now",
        like_counts=0,
    )
    parent = Comment(
        id="parent",
        text="父评论",
        user_name="评论者",
        user_id="2",
        user_avatar="",
        created_at="now",
        like_counts=0,
        replies=[reply],
    )

    records = comments_to_records("A", [parent])

    assert [item.id for item in records] == ["parent", "reply"]
    assert "replies" not in records[0].payload
    assert "replies" not in records[1].payload
    assert records[1].parent_id == "parent"


def test_media_is_promoted_only_with_committed_database_and_failure_rolls_back(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    source = FakeSource([page("A")], {"A": post("A")})
    if sync_module._SUPPORTS_DIRECTORY_FDS:
        real_link = sync_module.os.link

        def fail_media(source_path, destination_path, **kwargs):
            if destination_path == "A.jpg":
                raise OSError("模拟媒体提升失败")
            return real_link(source_path, destination_path, **kwargs)

        monkeypatch.setattr(sync_module.os, "link", fail_media)
    else:
        real_open = sync_module.os.open

        def fail_media(path, flags, *args, **kwargs):
            if (
                Path(path).name == "A.jpg"
                and flags & os.O_WRONLY
                and flags & os.O_EXCL
            ):
                raise OSError("模拟媒体提升失败")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(sync_module.os, "open", fail_media)
    with pytest.raises(WeiboError, match="媒体"):
        PersonalArchiveSync(
            root, source, IdentityProvider(), media_stager=FakeMediaStager()
        ).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    assert repo._connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    repo.close()
    assert not (root / "media" / "A.jpg").exists()

    monkeypatch.undo()
    PersonalArchiveSync(
        root, source, IdentityProvider(), media_stager=FakeMediaStager()
    ).run("incremental")
    assert (root / "media" / "A.jpg").read_bytes() == b"A"


def test_rebuild_refuses_symlink_target(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    require_symlink_capability(target_is_directory=True)
    actual = tmp_path / "actual"
    seed_archive(actual, record("old"))
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(WeiboError, match="符号链接"):
        PersonalArchiveSync(
            linked, FakeSource([], {}), IdentityProvider()
        ).run("rebuild")


def test_incremental_rejects_work_symlink_without_writing_external_directory(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    seed_archive(root)
    external = tmp_path / "external-work"
    external.mkdir()
    (root / ".work").symlink_to(external, target_is_directory=True)

    with pytest.raises(WeiboError, match=r"\.work|\u7b26\u53f7\u94fe\u63a5"):
        PersonalArchiveSync(
            root, FakeSource([page("A")], {"A": post("A")}), IdentityProvider()
        ).run("incremental")
    assert list(external.iterdir()) == []


def test_media_destination_symlink_is_rejected_without_external_write(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    seed_archive(root)
    external = tmp_path / "external-media"
    external.mkdir()
    (root / "media").symlink_to(external, target_is_directory=True)

    with pytest.raises(WeiboError, match="媒体.*符号链接"):
        PersonalArchiveSync(
            root,
            FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(),
            media_stager=FakeMediaStager(),
        ).run("incremental")
    assert list(external.iterdir()) == []
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    repo.close()


def test_staged_media_hardlink_is_rejected_without_touching_external_file(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync, StagedMedia

    root = tmp_path / "archive"
    seed_archive(root)
    external = tmp_path / "external.jpg"
    external.write_bytes(b"external")

    class HardlinkStager:
        def stage(
            self, post_value, comments, work_root, cancel_requested=None
        ):
            staged = work_root / "hardlink.jpg"
            os.link(external, staged)
            return [
                StagedMedia(
                    MediaRecord(
                        "post", "A", "image", 0,
                        "https://example.test/a.jpg", "media/A.jpg"
                    ),
                    staged,
                )
            ]

    with pytest.raises(WeiboError, match="硬链接"):
        PersonalArchiveSync(
            root,
            FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(),
            media_stager=HardlinkStager(),
        ).run("incremental")
    assert external.read_bytes() == b"external"
    assert external.stat().st_nlink == 1
    assert not (root / "media" / "A.jpg").exists()


@pytest.mark.parametrize(
    "target",
    ["data/archive.db", "manifest.json", ".work/other-run/file", "assets/a.jpg"],
)
def test_media_target_cannot_address_archive_control_files(tmp_path, target):
    from weibo_book.archive.sync import PersonalArchiveSync, StagedMedia

    root = tmp_path / "archive"
    seed_archive(root)
    before_manifest = (root / "manifest.json").read_bytes()

    class ControlTargetStager:
        def stage(self, post_value, comments, work_root, cancel_requested=None):
            staged = work_root / "payload"
            staged.write_bytes(b"malicious")
            return [
                StagedMedia(
                    MediaRecord("post", "A", "image", 0, "u", target),
                    staged,
                )
            ]

    with pytest.raises(WeiboError, match="media"):
        PersonalArchiveSync(
            root,
            FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(),
            media_stager=ControlTargetStager(),
    ).run("incremental")
    assert (root / "manifest.json").read_bytes() == before_manifest
    repository = ArchiveRepository.open(root, "10001")
    try:
        assert repository.get_post("A") is None
        assert repository._connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
    finally:
        repository.close()


@pytest.mark.parametrize("concurrent_change", ["new_file", "replace_file", "replace_directory"])
def test_media_promotion_aborts_when_target_changes_after_prepare(
    tmp_path, monkeypatch, concurrent_change
):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    media = root / "media"
    media.mkdir()
    target = media / "A.jpg"
    if concurrent_change != "new_file":
        target.write_bytes(b"old")
    original = PersonalArchiveSync._apply_promotion

    def change_then_apply(self, repository, run_id, checkpoint, promotion):
        if concurrent_change == "new_file":
            target.write_bytes(b"concurrent")
        elif concurrent_change == "replace_file":
            replacement = media / "replacement"
            replacement.write_bytes(b"concurrent")
            os.replace(replacement, target)
        else:
            target.unlink()
            target.mkdir()
        return original(self, repository, run_id, checkpoint, promotion)

    monkeypatch.setattr(
        PersonalArchiveSync, "_apply_promotion", change_then_apply
    )
    with pytest.raises(WeiboError, match="媒体目标.*(?:新建|变化)"):
        PersonalArchiveSync(
            root,
            FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(),
            media_stager=FakeMediaStager(),
        ).run("incremental")
    if concurrent_change == "replace_directory":
        assert target.is_dir()
    else:
        assert target.read_bytes() == b"concurrent"
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    assert repo._connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    repo.close()


def test_present_target_swap_inside_atomic_capture_preserves_concurrent_bytes(
    tmp_path, monkeypatch
):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    media = root / "media"
    media.mkdir()
    target = media / "A.jpg"
    target.write_bytes(b"old")
    original_rename = sync_module.os.rename
    original_replace = sync_module.os.replace
    swapped = False

    def swap_then_rename(source, destination, **kwargs):
        nonlocal swapped
        if not swapped and source == "A.jpg" and str(destination).startswith("promotion-backup-"):
            swapped = True
            replacement = media / "concurrent"
            replacement.write_bytes(b"concurrent")
            os.replace(replacement, target)
        return original_rename(source, destination, **kwargs)

    def swap_then_replace(source, destination, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and Path(source) == target
            and Path(destination).name.startswith("promotion-backup-")
        ):
            swapped = True
            replacement = media / "concurrent"
            replacement.write_bytes(b"concurrent")
            original_replace(replacement, target)
        return original_replace(source, destination, **kwargs)

    if sync_module._SUPPORTS_DIRECTORY_FDS:
        monkeypatch.setattr(sync_module.os, "rename", swap_then_rename)
    else:
        monkeypatch.setattr(sync_module.os, "replace", swap_then_replace)
    with pytest.raises(WeiboError, match="原子夺取.*变化"):
        PersonalArchiveSync(
            root, FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(), media_stager=FakeMediaStager(),
        ).run("incremental")
    assert target.read_bytes() == b"concurrent"


def test_missing_target_creation_inside_atomic_install_is_never_overwritten(
    tmp_path, monkeypatch
):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    media = root / "media"
    media.mkdir()
    target = media / "A.jpg"
    original_link = sync_module.os.link
    original_open = sync_module.os.open
    swapped = False

    def create_then_link(source, destination, **kwargs):
        nonlocal swapped
        if not swapped and destination == "A.jpg":
            swapped = True
            target.write_bytes(b"concurrent")
        return original_link(source, destination, **kwargs)

    def create_then_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and Path(path) == target
            and flags & os.O_CREAT
            and flags & os.O_EXCL
        ):
            swapped = True
            target.write_bytes(b"concurrent")
        return original_open(path, flags, *args, **kwargs)

    if sync_module._SUPPORTS_DIRECTORY_FDS:
        monkeypatch.setattr(sync_module.os, "link", create_then_link)
    else:
        monkeypatch.setattr(sync_module.os, "open", create_then_open)
    with pytest.raises(WeiboError, match="第三方新建"):
        PersonalArchiveSync(
            root, FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(), media_stager=FakeMediaStager(),
        ).run("incremental")
    assert target.read_bytes() == b"concurrent"


def test_windows_atomic_create_uses_exclusive_open_and_removes_only_created_target(
    tmp_path, monkeypatch
):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    staged = tmp_path / "staged"
    staged.write_bytes(b"payload")
    target = tmp_path / "target"
    opened_flags = []

    def fake_open(path, flags, mode=0o777, **kwargs):
        opened_flags.append(flags)
        if path == target:
            target.write_bytes(b"partial")
            return 12
        return 11

    monkeypatch.setattr(sync_module.os, "name", "nt")
    monkeypatch.setattr(sync_module.os, "open", fake_open)
    monkeypatch.setattr(
        sync_module.os, "read", lambda descriptor, size: (_ for _ in ()).throw(OSError("copy failed"))
    )
    monkeypatch.setattr(sync_module.os, "close", lambda descriptor: None)

    with pytest.raises(OSError, match="copy failed"):
        PersonalArchiveSync._install_staged_without_overwrite(
            target, staged, 99, target.name
        )
    assert any(
        flags & os.O_CREAT and flags & os.O_EXCL
        for flags in opened_flags
    )
    assert not target.exists()
    assert staged.read_bytes() == b"payload"


@pytest.mark.parametrize("corruption", ["other_run", "duplicate_target", "bad_hash"])
def test_promotion_recovery_rejects_unbound_or_duplicate_journal_paths(
    tmp_path, corruption
):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    repo = ArchiveRepository.open(root, "10001")
    run_id = repo.begin_sync("incremental")
    entries = [
        {
            "staged": f".work/{run_id}/safe-1",
            "target": "media/a.jpg",
            "backup": f".work/{run_id}/backup-1",
            "expected_target": {"state": "missing"},
            "installed_target": None,
            "install_proof": {
                "sha256": "0" * 64, "size": 0,
                "staged_dev": 0, "staged_ino": 0,
            },
            "step": "prepared",
        }
    ]
    if corruption == "other_run":
        entries[0]["staged"] = ".work/other-run/safe-1"
    elif corruption == "duplicate_target":
        entries.append(
            {
                "staged": f".work/{run_id}/safe-2",
                "target": "media/a.jpg",
                "backup": f".work/{run_id}/backup-2",
                "expected_target": {"state": "missing"},
                "installed_target": None,
                "install_proof": {
                    "sha256": "0" * 64, "size": 0,
                    "staged_dev": 0, "staged_ino": 0,
                },
                "step": "prepared",
            }
        )
    else:
        entries[0]["install_proof"]["sha256"] = "not-a-sha256"
    repo.update_sync_checkpoint(
        run_id,
        {
            "pending_bids": ["A"],
            "completed_bids": [],
            "new_bids": ["A"],
            "refresh_bids": [],
            "counters": {},
            "promotion": {
                "run_id": run_id,
                "phase": "prepared",
                "entries": entries,
            },
        },
    )
    repo.finish_sync(run_id, "error", {})
    repo.close()
    before_manifest = (root / "manifest.json").read_bytes()
    before_database = database_hash(root)

    with pytest.raises(WeiboError, match="日志.*(?:路径|内容证明)|目标.*重复"):
        PersonalArchiveSync(
            root, FakeSource([], {}), IdentityProvider()
        ).run("incremental")
    assert (root / "manifest.json").read_bytes() == before_manifest
    assert database_hash(root) == before_database
    ArchiveRepository.open(root, "10001").close()


def test_promotion_recovery_keeps_backup_when_third_party_recreates_target(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    (root / "media").mkdir()
    target = root / "media" / "A.jpg"
    target.write_bytes(b"original")
    marker = target.lstat()
    expected = PersonalArchiveSync._target_state(marker)
    repo = ArchiveRepository.open(root, "10001")
    run_id = repo.begin_sync("incremental")
    work = root / ".work" / run_id
    work.mkdir(parents=True)
    backup = work / "promotion-backup-0"
    os.rename(target, backup)
    target.write_bytes(b"B")
    promotion = {
        "run_id": run_id,
        "phase": "prepared",
        "entries": [{
            "staged": f".work/{run_id}/promotion-safe-0",
            "target": "media/A.jpg",
            "backup": f".work/{run_id}/promotion-backup-0",
            "expected_target": expected,
            "installed_target": None,
            "install_proof": {
                "sha256": hashlib.sha256(b"A").hexdigest(),
                "size": 1, "staged_dev": 0, "staged_ino": 0,
            },
            "step": "backup_captured",
        }],
    }
    repo.update_sync_checkpoint(run_id, {
        "completed_bids": [], "counters": {}, "promotion": promotion,
    })
    repo.finish_sync(run_id, "error", {})
    repo.close()

    with pytest.raises(WeiboError, match="第三方新建.*备份.*保留"):
        PersonalArchiveSync(
            root, FakeSource([], {}), IdentityProvider()
        ).run("incremental")
    assert target.read_bytes() == b"B"
    assert backup.read_bytes() == b"original"
    repo = ArchiveRepository.open(root, "10001")
    saved = repo.get_unfinished_sync("incremental")
    assert saved is not None
    assert saved.checkpoint["promotion"] is not None
    repo.close()


@pytest.mark.parametrize("phase", ["prepared", "promoted", "db_committed"])
@pytest.mark.parametrize("supports_directory_fds", [False])
def test_promotion_journal_recovers_each_failed_phase(
    tmp_path, monkeypatch, phase, supports_directory_fds
):
    from weibo_book.archive import repository as repository_module
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    (root / "media").mkdir()
    target = root / "media" / "A.jpg"
    target.write_bytes(b"old")
    source = FakeSource([page("A")], {"A": post("A")})
    monkeypatch.setattr(
        sync_module, "_SUPPORTS_DIRECTORY_FDS", supports_directory_fds
    )
    original = repository_module.ArchiveRepository.update_sync_checkpoint
    failed = False

    def fail_phase(self, run_id, checkpoint):
        nonlocal failed
        promotion = checkpoint.get("promotion")
        if (
            not failed
            and isinstance(promotion, dict)
            and promotion.get("phase") == phase
        ):
            failed = True
            raise WeiboError(f"模拟 promotion {phase} 失败")
        return original(self, run_id, checkpoint)

    monkeypatch.setattr(
        repository_module.ArchiveRepository,
        "update_sync_checkpoint",
        fail_phase,
    )
    with pytest.raises(WeiboError, match="promotion"):
        PersonalArchiveSync(
            root,
            source,
            IdentityProvider(),
            media_stager=FakeMediaStager(),
        ).run("incremental")
    assert target.read_bytes() == b"old"
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    assert repo._connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    repo.close()

    source.fetches.clear()
    result = PersonalArchiveSync(
        root,
        source,
        IdentityProvider(),
        media_stager=FakeMediaStager(),
    ).run("incremental")
    assert result.new_posts == 1
    assert target.read_bytes() == b"A"


def test_staged_path_swap_after_open_copies_verified_inode(
    tmp_path, monkeypatch
):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    if not sync_module._SUPPORTS_DIRECTORY_FDS and os.name == "nt":
        pytest.skip("Windows 禁止改名本进程仍持有的已打开文件")

    root = tmp_path / "archive"
    seed_archive(root)
    external = tmp_path / "external.jpg"
    external.write_bytes(b"outside")
    state = {"staged": None, "swapped": False}

    class SwapStager(FakeMediaStager):
        def stage(self, *args, cancel_requested=None):
            values = super().stage(*args, cancel_requested=cancel_requested)
            state["staged"] = values[0].staged_path
            return values

    real_open = sync_module.os.open

    def swap_after_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        expected_path = "A.jpg" if sync_module._SUPPORTS_DIRECTORY_FDS else state["staged"]
        if (
            not state["swapped"]
            and path == expected_path
            and not flags & getattr(os, "O_DIRECTORY", 0)
            and state["staged"] is not None
        ):
            staged_path = state["staged"]
            staged_path.rename(staged_path.with_name("opened-original.jpg"))
            shutil.copy2(external, staged_path)
            state["swapped"] = True
        return descriptor

    monkeypatch.setattr(sync_module.os, "open", swap_after_open)
    if sync_module._SUPPORTS_DIRECTORY_FDS:
        PersonalArchiveSync(
            root,
            FakeSource([page("A")], {"A": post("A")}),
            IdentityProvider(),
            media_stager=SwapStager(),
        ).run("incremental")
        assert (root / "media" / "A.jpg").read_bytes() == b"A"
    else:
        with pytest.raises(WeiboError, match="媒体暂存文件在打开时已变化"):
            PersonalArchiveSync(
                root,
                FakeSource([page("A")], {"A": post("A")}),
                IdentityProvider(),
                media_stager=SwapStager(),
            ).run("incremental")
        assert not (root / "media" / "A.jpg").exists()

    assert state["swapped"] is True
    assert external.read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("crash_window", "exit_code"),
    [("after_install", 73), ("after_installed_checkpoint", 74)],
)
def test_process_crash_during_media_install_is_recovered_on_next_run(
    tmp_path, crash_window, exit_code
):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    (root / "media").mkdir()
    target = root / "media" / "A.jpg"
    target.write_bytes(b"old")
    code = r'''
import os
from datetime import datetime, timezone
from pathlib import Path
from weibo_book.archive.discovery import ProfileItem, ProfilePage
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import MediaRecord
from weibo_book.archive.sync import PersonalArchiveSync, StagedMedia
from weibo_book.models import Post

root = Path(os.environ["ARCHIVE_ROOT"])
class Identity:
    def whoami(self):
        return {"uid": "10001", "screen_name": "测试用户"}
class Source:
    def iter_profile_pages(self, uid):
        yield ProfilePage([ProfileItem("A")], True)
    def fetch_post(self, uid, bid):
        return Post(bid="A", uid="10001", user_name="u", user_avatar="", text="new", created_at=datetime.now(timezone.utc))
    def fetch_recent_comments(self, post_id, limit=10):
        return []
class Stager:
    def stage(self, post, comments, work_root, cancel_requested=None):
        staged = work_root / "A.jpg"
        staged.write_bytes(b"A")
        return [StagedMedia(MediaRecord("post", "A", "image", 0, "u", "media/A.jpg"), staged)]
window = os.environ["CRASH_WINDOW"]
if window == "after_install":
    original_install = PersonalArchiveSync._install_staged_without_overwrite
    def crash_after_install(*args, **kwargs):
        original_install(*args, **kwargs)
        os._exit(73)
    PersonalArchiveSync._install_staged_without_overwrite = staticmethod(crash_after_install)
else:
    original_checkpoint = ArchiveRepository.update_sync_checkpoint
    def crash_after_checkpoint(self, run_id, checkpoint):
        result = original_checkpoint(self, run_id, checkpoint)
        promotion = checkpoint.get("promotion")
        if isinstance(promotion, dict):
            entries = promotion.get("entries")
            if isinstance(entries, list) and entries and entries[0].get("step") == "installed":
                os._exit(74)
        return result
    ArchiveRepository.update_sync_checkpoint = crash_after_checkpoint
PersonalArchiveSync(root, Source(), Identity(), media_stager=Stager()).run("incremental")
'''
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "ARCHIVE_ROOT": str(root),
            "CRASH_WINDOW": crash_window,
        },
        timeout=20,
        check=False,
    )
    assert completed.returncode == exit_code
    assert target.read_bytes() == b"A"

    failing = FakeSource([page("A")], {"A": post("A")})
    failing.failures["A"] = WeiboError("断网", kind=WeiboErrorKind.NETWORK)
    with pytest.raises(WeiboError):
        PersonalArchiveSync(
            root, failing, IdentityProvider(), media_stager=FakeMediaStager()
        ).run("incremental")
    assert target.read_bytes() == b"old"
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_post("A") is None
    repo.close()

    successful = PersonalArchiveSync(
        root,
        FakeSource([page("A")], {"A": post("A")}),
        IdentityProvider(),
        media_stager=FakeMediaStager(),
    ).run("incremental")
    assert successful.new_posts == 1
    assert target.read_bytes() == b"A"


def test_second_rebuild_rename_and_rollback_failure_self_heals_next_run(
    tmp_path, monkeypatch
):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, _rebuild_state_path

    root = tmp_path / "archive"
    seed_archive(root, record("old"))
    source = FakeSource([page("new")], {"new": post("new")})
    real_replace = sync_module.os.replace

    def fail_second_and_rollback(source_path, destination_path, **kwargs):
        source_name = Path(source_path).name
        if Path(destination_path) == root and (
            ".rebuild-" in source_name or ".previous-" in source_name
        ):
            raise OSError("模拟重建 rename 失败")
        return real_replace(source_path, destination_path, **kwargs)

    monkeypatch.setattr(sync_module.os, "replace", fail_second_and_rollback)
    with pytest.raises(WeiboError, match="回滚失败"):
        PersonalArchiveSync(root, source, IdentityProvider()).run("rebuild")
    assert not root.exists()
    assert _rebuild_state_path(root).is_file()
    assert list(tmp_path.glob(".archive.previous-*"))

    monkeypatch.setattr(sync_module.os, "replace", real_replace)
    PersonalArchiveSync(root, source, IdentityProvider()).run("rebuild")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.list_known_bids() == {"new"}
    repo.close()
    assert not _rebuild_state_path(root).exists()
    assert not list(tmp_path.glob(".archive.previous-*"))


@pytest.mark.parametrize("phase", ["prepared", "backup_moved", "temp_promoted"])
@pytest.mark.parametrize("direction", ["alias_to_real", "real_to_alias"])
def test_rebuild_journal_recovers_each_rename_window(tmp_path, phase, direction):
    from weibo_book.archive.sync import (
        PersonalArchiveSync,
        _rebuild_state_path,
        _write_state_atomic,
    )

    require_symlink_capability(target_is_directory=True)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    root = real_parent / "archive"
    alias_root = alias_parent / "archive"
    seed_archive(root, record("old"))
    temporary = real_parent / ".archive.rebuild-test"
    seed_archive(temporary, record("new"))
    backup = real_parent / ".archive.previous-test"
    if phase in {"backup_moved", "temp_promoted"}:
        os.replace(root, backup)
    if phase == "temp_promoted":
        os.replace(temporary, root)
    writer_root = alias_root if direction == "alias_to_real" else root
    recovery_root = root if direction == "alias_to_real" else alias_root
    writer = PersonalArchiveSync(writer_root, FakeSource([], {}), IdentityProvider())
    _write_state_atomic(
        _rebuild_state_path(writer_root),
        writer._rebuild_state(temporary, backup, phase, "10001"),
    )

    PersonalArchiveSync(
        recovery_root,
        FakeSource([page()], {}),
        IdentityProvider(),
    ).run("incremental")
    repo = ArchiveRepository.open(root, "10001")
    assert repo.list_known_bids() == ({"new"} if phase == "temp_promoted" else {"old"})
    repo.close()
    assert not _rebuild_state_path(root).exists()
    assert not backup.exists()
    assert not temporary.exists()


def test_second_process_cannot_start_while_archive_lock_is_held(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    ready = tmp_path / "ready"
    code = r'''
import os
import time
from pathlib import Path
from weibo_book.archive.sync import _archive_lock
root = Path(os.environ["ARCHIVE_ROOT"])
ready = Path(os.environ["READY_PATH"])
with _archive_lock(root):
    ready.write_text("ready", encoding="utf-8")
    time.sleep(10)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "ARCHIVE_ROOT": str(root),
            "READY_PATH": str(ready),
        },
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        started = time.monotonic()
        with pytest.raises(WeiboError, match="正在备份"):
            PersonalArchiveSync(
                root, FakeSource([page()], {}), IdentityProvider()
            ).run("incremental")
        assert time.monotonic() - started < 1
        repo = ArchiveRepository.open(root, "10001")
        assert repo._connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
        repo.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_archive_lock_is_shared_through_symlinked_parent_alias(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    require_symlink_capability(target_is_directory=True)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    root = real_parent / "archive"
    alias_root = alias_parent / "archive"
    seed_archive(root)
    ready = tmp_path / "ready-alias"
    code = r'''
import os
import time
from pathlib import Path
from weibo_book.archive.sync import _archive_lock
root = Path(os.environ["ARCHIVE_ROOT"])
ready = Path(os.environ["READY_PATH"])
with _archive_lock(root):
    ready.write_text("ready", encoding="utf-8")
    time.sleep(10)
'''
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "ARCHIVE_ROOT": str(root),
            "READY_PATH": str(ready),
        },
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(WeiboError, match="正在备份"):
            PersonalArchiveSync(
                alias_root, FakeSource([page()], {}), IdentityProvider()
            ).run("incremental")
        repo = ArchiveRepository.open(root, "10001")
        assert repo._connection.execute(
            "SELECT COUNT(*) FROM sync_runs"
        ).fetchone()[0] == 0
        repo.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("manifest_written", [False, True])
def test_committing_sync_is_reconciled_after_process_death_without_refetch(
    tmp_path, manifest_written
):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    successful_at = "2026-07-14T01:02:03+00:00"
    code = r'''
import os
from pathlib import Path
from weibo_book.archive.repository import ArchiveRepository
root = Path(os.environ["ARCHIVE_ROOT"])
repo = ArchiveRepository.open(root, "10001")
run_id = repo.begin_sync("incremental")
(root / ".work" / run_id).mkdir(parents=True)
summary = {
    "new_posts": 1,
    "refreshed_posts": 2,
    "changed_posts": 3,
    "unavailable_posts": 4,
    "generated_files": ["weibo.html"],
    "resumed": False,
}
checkpoint = {
    "successful_at": os.environ["SUCCESSFUL_AT"],
    "completion_summary": summary,
}
repo.mark_sync_committing(run_id, checkpoint, summary)
if os.environ["MANIFEST_WRITTEN"] == "1":
    repo.update_manifest_success(os.environ["SUCCESSFUL_AT"])
os._exit(91)
'''
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "ARCHIVE_ROOT": str(root),
            "SUCCESSFUL_AT": successful_at,
            "MANIFEST_WRITTEN": "1" if manifest_written else "0",
        },
        timeout=5,
        check=False,
    )
    assert process.returncode == 91

    class NoFetchSource:
        def iter_profile_pages(self, uid):
            raise AssertionError("恢复提交不应重新发起网络抓取")

        def fetch_post(self, uid, bid):
            raise AssertionError("恢复提交不应重新发起网络抓取")

        def fetch_recent_comments(self, post_id, limit=10):
            raise AssertionError("恢复提交不应重新发起网络抓取")

    result = PersonalArchiveSync(
        root, NoFetchSource(), IdentityProvider()
    ).run("incremental")
    assert result.new_posts == 1
    assert result.refreshed_posts == 2
    assert result.changed_posts == 3
    assert result.unavailable_posts == 4
    repo = ArchiveRepository.open(root, "10001")
    assert repo.manifest().last_successful_sync_at == successful_at
    assert repo._connection.execute(
        "SELECT status FROM sync_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0] == "done"
    run_id = repo._connection.execute(
        "SELECT run_id FROM sync_runs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert not (root / ".work" / run_id).exists()
    repo.close()


def test_committing_cleanup_never_follows_run_work_symlink(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    seed_archive(root)
    repo = ArchiveRepository.open(root, "10001")
    run_id = repo.begin_sync("incremental")
    summary = {
        "new_posts": 0, "refreshed_posts": 0, "changed_posts": 0,
        "unavailable_posts": 0, "generated_files": [], "resumed": False,
    }
    checkpoint = {
        "successful_at": "2026-07-14T01:02:03+00:00",
        "completion_summary": summary,
        "promotion": None,
    }
    repo.mark_sync_committing(run_id, checkpoint, summary)
    repo.close()
    external = tmp_path / "external-work"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    work = root / ".work"
    work.mkdir()
    (work / run_id).symlink_to(external, target_is_directory=True)

    with pytest.raises(WeiboError, match="暂存目录不安全"):
        PersonalArchiveSync(
            root, FakeSource([], {}), IdentityProvider()
        ).run("incremental")
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (work / run_id).is_symlink()


@pytest.mark.parametrize("direction", ["alias_to_real", "real_to_alias"])
def test_create_empty_directory_is_recovered_after_death_between_rmdir_and_replace(
    tmp_path, direction,
):
    from weibo_book.archive.sync import PersonalArchiveSync, _rebuild_state_path

    require_symlink_capability(target_is_directory=True)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    root = real_parent / "archive"
    alias_root = alias_parent / "archive"
    root.mkdir()
    temporary = real_parent / ".archive.create-crash"
    prepared = ArchiveRepository.create(temporary, "10001", "测试用户")
    prepared.close()
    writer_root = alias_root if direction == "alias_to_real" else root
    recovery_root = root if direction == "alias_to_real" else alias_root
    code = r'''
import os
from pathlib import Path
from weibo_book.archive.sync import PersonalArchiveSync
root = Path(os.environ["ARCHIVE_ROOT"])
temporary = Path(os.environ["TEMPORARY"])
service = PersonalArchiveSync(root, None, None)
original = os.replace
def crash(source, target, *args, **kwargs):
    if Path(source) == temporary and Path(target) == root:
        os._exit(92)
    return original(source, target, *args, **kwargs)
os.replace = crash
service._replace_formal_directory(temporary, "create", "10001")
'''
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "ARCHIVE_ROOT": str(writer_root),
            "TEMPORARY": str(temporary),
        },
        timeout=5,
        check=False,
    )
    assert process.returncode == 92
    assert not root.exists()
    assert temporary.is_dir()
    assert _rebuild_state_path(root).is_file()

    result = PersonalArchiveSync(
        recovery_root, FakeSource([], {}), IdentityProvider()
    ).run("incremental")
    assert result.new_posts == 0
    assert root.is_dir()
    assert not temporary.exists()
    assert not _rebuild_state_path(root).exists()
    repo = ArchiveRepository.open(root, "10001")
    assert repo._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    repo.close()


def test_archive_lock_symlink_is_rejected(tmp_path):
    from weibo_book.archive.sync import (
        PersonalArchiveSync,
        _archive_lock_path,
    )

    require_symlink_capability(target_is_directory=False)
    root = tmp_path / "archive"
    seed_archive(root)
    external = tmp_path / "external-lock"
    external.write_text("outside", encoding="utf-8")
    lock_path = _archive_lock_path(root)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    lock_path.symlink_to(external)

    with pytest.raises(WeiboError, match="锁文件.*符号链接"):
        PersonalArchiveSync(
            root, FakeSource([page()], {}), IdentityProvider()
        ).run("incremental")
    assert external.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize(
    ("uid", "screen_name"),
    [
        (None, "测试用户"),
        (10001, "测试用户"),
        ("", "测试用户"),
        ("   ", "测试用户"),
        ("10001", None),
        ("10001", 123),
        ("10001", ""),
        ("10001", "   "),
    ],
)
def test_identity_fields_must_be_nonempty_strings_without_coercion(
    tmp_path, uid, screen_name
):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"

    class InvalidIdentity:
        def whoami(self):
            return {"uid": uid, "screen_name": screen_name}

    with pytest.raises(WeiboError, match="登录账号信息"):
        PersonalArchiveSync(
            root, FakeSource([], {}), InvalidIdentity()
        ).run("create")
    assert not root.exists()


@pytest.mark.parametrize("identity", [{"screen_name": "测试用户"}, {"uid": "10001"}])
def test_identity_missing_required_exact_key_is_rejected(tmp_path, identity):
    from weibo_book.archive.sync import PersonalArchiveSync

    class MissingIdentity:
        def whoami(self):
            return identity

    root = tmp_path / "archive"
    with pytest.raises(WeiboError, match="登录账号信息"):
        PersonalArchiveSync(
            root, FakeSource([], {}), MissingIdentity()
        ).run("create")
    assert not root.exists()


def test_work_directory_uses_checked_path_fallback_without_directory_descriptors(
    tmp_path, monkeypatch
):
    import weibo_book.archive.sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    root.mkdir()
    run_id = "01234567-89ab-4cde-8fab-0123456789ab"
    original_open = sync_module.os.open
    monkeypatch.setattr(
        sync_module, "_SUPPORTS_DIRECTORY_FDS", False, raising=False
    )

    def reject_directory_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is None and Path(path).is_dir():
            raise PermissionError("Windows 不支持以 POSIX 方式打开目录")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sync_module.os, "open", reject_directory_open)
    sync = PersonalArchiveSync(root, FakeSource([], {}), IdentityProvider())

    sync._prepare_work_root(root, run_id)
    assert (root / ".work" / run_id).is_dir()

    sync._cleanup_empty_run_work(root, run_id)
    assert not (root / ".work" / run_id).exists()


def test_staged_media_copy_uses_checked_path_fallback_without_directory_descriptors(
    tmp_path, monkeypatch
):
    import weibo_book.archive.sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    work_root = tmp_path / "work"
    work_root.mkdir()
    staged = work_root / "A.jpg"
    staged.write_bytes(b"media")
    original_open = sync_module.os.open
    monkeypatch.setattr(sync_module, "_SUPPORTS_DIRECTORY_FDS", False)

    def reject_directory_open(path, flags, *args, **kwargs):
        if flags & sync_module._O_DIRECTORY:
            raise PermissionError("Windows 不支持以 POSIX 方式打开目录")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sync_module.os, "open", reject_directory_open)
    sync = PersonalArchiveSync(tmp_path / "archive", FakeSource([], {}), IdentityProvider())

    copied = sync._copy_staged_to_safe(work_root, staged, 0)

    assert copied.read_bytes() == b"media"


def test_sync_promotes_staged_media_without_directory_descriptors(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    seed_archive(root)
    original_open = sync_module.os.open
    monkeypatch.setattr(sync_module, "_SUPPORTS_DIRECTORY_FDS", False)

    def reject_directory_open(path, flags, *args, **kwargs):
        caller = inspect.currentframe().f_back
        if (
            flags & sync_module._O_DIRECTORY
            and caller is not None
            and caller.f_code.co_filename == sync_module.__file__
        ):
            raise PermissionError("Windows 不支持以 POSIX 方式打开目录")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sync_module.os, "open", reject_directory_open)

    result = PersonalArchiveSync(
        root,
        FakeSource([page("A")], {"A": post("A")}),
        IdentityProvider(),
        media_stager=FakeMediaStager(),
    ).run("incremental")

    assert result.new_posts == 1
    assert (root / "media" / "A.jpg").read_bytes() == b"A"
