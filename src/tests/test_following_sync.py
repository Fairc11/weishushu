from __future__ import annotations

from datetime import datetime, timezone

import pytest

from weibo_book.archive.following import FollowingObjectRecord
from weibo_book.archive.following_source import BloggerPage, FollowingListResult
from weibo_book.errors import OperationPaused, WeiboError


def _blogger(identity: str, name: str, order: int) -> FollowingObjectRecord:
    return FollowingObjectRecord(
        "blogger", identity, name, f"https://weibo.com/u/{identity}", "", order
    )


def _topic(identity: str, name: str, order: int) -> FollowingObjectRecord:
    return FollowingObjectRecord(
        "supertopic", identity, name, f"//weibo.com/p/{identity}",
        f"sinaweibo://pageinfo?containerid={identity}", order,
    )


def _checkpoint(snapshot_id: str) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "blogger_next_page": 1,
        "blogger_next_cursor": None,
        "blogger_completed_count": 0,
        "bloggers_done": False,
        "supertopics_done": False,
        "blogger_reported_total": None,
        "supertopic_reported_total": None,
    }


class Source:
    def __init__(self, blogger_pages, topics):
        self.blogger_pages = list(blogger_pages)
        self.topics = topics
        self.calls = []

    def fetch_blogger_page(self, *, page, next_cursor, source_offset):
        self.calls.append(("blogger", page, next_cursor, source_offset))
        return self.blogger_pages.pop(0)

    def fetch_supertopics(self):
        self.calls.append(("supertopic",))
        return self.topics


def _repository(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    return ArchiveRepository.create(tmp_path / "微博书", "10001", "本人")


def test_sync_stages_both_lists_then_commits_complete_snapshot(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    checkpoint = _checkpoint(snapshot_id)
    saved = []
    phases = []
    source = Source(
        [BloggerPage([_blogger("1", "甲", 0), _blogger("2", "乙", 1)], 2, 0, False)],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    result = FollowingArchiveSync(
        repository,
        source,
        checkpoint_saved=lambda value: (checkpoint.update(value), saved.append(dict(value))),
        phase_changed=phases.append,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(checkpoint)

    assert result.blogger_count == 2
    assert result.supertopic_count == 1
    assert repository.get_current_following_snapshot().snapshot_id == snapshot_id
    assert [item.object_id for item in repository.list_following_snapshot_items()] == ["1", "2", "1022:1"]
    assert checkpoint["bloggers_done"] is True
    assert checkpoint["supertopics_done"] is True
    assert checkpoint["blogger_reported_total"] == 2
    assert checkpoint["supertopic_reported_total"] == 1
    assert phases == ["bloggers", "supertopics", "duration"]
    assert len(saved) == 2
    repository.close()


def test_resume_replays_page_staged_before_checkpoint_without_duplicate(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    checkpoint = _checkpoint(snapshot_id)
    first_page = BloggerPage([_blogger("1", "甲", 0)], 2, 50, False)
    repository.stage_following_items(snapshot_id, first_page.items)
    second_page = BloggerPage([_blogger("2", "乙", 1)], 2, 0, False)
    source = Source(
        [first_page, second_page],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    FollowingArchiveSync(
        repository,
        source,
        checkpoint_saved=checkpoint.update,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(checkpoint)

    assert source.calls[:2] == [
        ("blogger", 1, None, 0),
        ("blogger", 2, 50, 1),
    ]
    assert [item.object_id for item in repository.list_following_snapshot_items(snapshot_id)] == ["1", "2", "1022:1"]
    repository.close()


def test_pause_after_first_page_keeps_staging_and_resume_uses_next_page(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    checkpoint = _checkpoint(snapshot_id)
    paused = False

    def save(value):
        nonlocal paused
        checkpoint.update(value)
        paused = True

    source = Source(
        [
            BloggerPage([_blogger("1", "甲", 0)], 2, 50, False),
            BloggerPage([_blogger("2", "乙", 1)], 2, 0, False),
        ],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )
    with pytest.raises(OperationPaused):
        FollowingArchiveSync(
            repository,
            source,
            checkpoint_saved=save,
            pause_requested=lambda: paused,
        ).run(checkpoint)

    assert checkpoint["blogger_next_page"] == 2
    assert checkpoint["blogger_next_cursor"] == 50
    assert checkpoint["blogger_completed_count"] == 1
    assert checkpoint["blogger_reported_total"] == 2
    FollowingArchiveSync(
        repository,
        source,
        checkpoint_saved=checkpoint.update,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(checkpoint)
    assert source.calls[1] == ("blogger", 2, 50, 1)
    repository.close()


def test_resume_rejects_reported_total_changed_after_interruption(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    checkpoint = _checkpoint(snapshot_id)
    paused = False

    def save(value):
        nonlocal paused
        checkpoint.update(value)
        paused = True

    first = Source(
        [BloggerPage([_blogger("1", "甲", 0)], 2, 50, False)],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )
    with pytest.raises(OperationPaused):
        FollowingArchiveSync(
            repository,
            first,
            checkpoint_saved=save,
            pause_requested=lambda: paused,
        ).run(checkpoint)

    changed = Source(
        [BloggerPage([_blogger("2", "乙", 1)], 3, 0, False)],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )
    with pytest.raises(WeiboError, match="跨页报告总数"):
        FollowingArchiveSync(repository, changed).run(checkpoint)

    assert repository.get_following_snapshot(snapshot_id).status == "staging"
    repository.close()


def test_normal_next_page_duplicate_is_not_treated_as_crash_replay(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    source = Source(
        [
            BloggerPage([_blogger("1", "甲", 0)], 1, 50, False),
            BloggerPage([_blogger("1", "甲", 1)], 1, 0, False),
        ],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    with pytest.raises(WeiboError, match="跨页出现重复"):
        FollowingArchiveSync(repository, source).run(_checkpoint(snapshot_id))

    assert repository.get_following_snapshot(snapshot_id).status == "staging"
    assert repository.get_current_following_snapshot() is None
    repository.close()


def test_low_intensity_uses_known_and_discovered_profile_request_count(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    elapsed = [0.0]

    def wait(seconds):
        elapsed[0] += seconds

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        monotonic=lambda: elapsed[0],
        uniform=lambda low, high: (low + high) / 2,
        wait=wait,
    )
    scheduler.set_known_request_counts(profile=2)
    source = Source(
        [
            BloggerPage([_blogger("1", "甲", 0)], 2, 50, False),
            BloggerPage([_blogger("2", "乙", 1)], 2, 0, False),
        ],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    FollowingArchiveSync(
        repository,
        source,
        pacing_scheduler=scheduler,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(_checkpoint(snapshot_id))

    assert elapsed[0] == pytest.approx(9000.0)
    repository.close()


def test_unconfirmed_update_commits_and_preserves_posts_and_relationship(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync
    from weibo_book.archive.schema import PostRecord

    repository = _repository(tmp_path)
    repository.upsert_post(PostRecord("BID1", "10001", "正文", "2026-07-18"))
    old = repository.begin_following_snapshot("2026-07-17T00:00:00+00:00")
    repository.stage_following_items(
        old,
        [_blogger("1", "甲", 0), _blogger("2", "乙", 1), _topic("1022:1", "超话甲", 0)],
    )
    repository.commit_following_snapshot(
        old, cutoff_at="2026-07-17T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=1,
    )
    current = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    source = Source(
        # 平台不返回「乙」：报告总数为 2，实际只返回 1 个，过滤标志为 true。
        [BloggerPage([_blogger("1", "甲", 0)], 2, 0, True)],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    result = FollowingArchiveSync(
        repository,
        source,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(_checkpoint(current))

    assert result.unfollowed_count == 0
    assert result.unconfirmed_count == 1
    assert repository.get_current_following_snapshot().snapshot_id == current
    snapshot = repository.get_following_snapshot(current)
    assert snapshot.summary["blogger_unconfirmed"] is True
    assert snapshot.summary["unconfirmed_bloggers"] == [
        {"object_id": "2", "name": "乙", "page_url": "https://weibo.com/u/2"}
    ]
    relationships = {
        item.object_id: item for item in repository.list_following_relationships()
    }
    assert relationships["2"].ended_snapshot_id is None
    assert repository.list_following_changes() == []
    assert repository.get_post("BID1").text == "正文"
    repository.close()


def test_unconfirmed_blogger_still_missing_in_complete_run_becomes_unfollowed(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    old = repository.begin_following_snapshot("2026-07-17T00:00:00+00:00")
    repository.stage_following_items(old, [_blogger("1", "甲", 0), _blogger("2", "乙", 1)])
    repository.commit_following_snapshot(
        old, cutoff_at="2026-07-17T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=0,
    )
    second = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    repository.stage_following_items(second, [_blogger("1", "甲", 0)])
    repository.commit_following_snapshot(
        second, cutoff_at="2026-07-18T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=0, blogger_unconfirmed=True,
    )
    third = repository.begin_following_snapshot("2026-07-19T00:00:00+00:00")
    source = Source(
        [BloggerPage([_blogger("1", "甲", 0)], 1, 0, False)],
        FollowingListResult([], 0, True),
    )

    result = FollowingArchiveSync(
        repository,
        source,
        now=lambda: datetime(2026, 7, 19, 1, tzinfo=timezone.utc),
    ).run(_checkpoint(third))

    assert result.unfollowed_count == 1
    assert result.unconfirmed_count == 0
    changes = repository.list_following_changes()
    assert [(item.change_type, item.object_id) for item in changes] == [
        ("unfollowed", "2")
    ]
    relationships = {
        item.object_id: item for item in repository.list_following_relationships()
    }
    assert relationships["2"].ended_snapshot_id == third
    repository.close()


def test_unconfirmed_blogger_reappearing_is_confirmed_without_change(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    old = repository.begin_following_snapshot("2026-07-17T00:00:00+00:00")
    repository.stage_following_items(old, [_blogger("1", "甲", 0), _blogger("2", "乙", 1)])
    repository.commit_following_snapshot(
        old, cutoff_at="2026-07-17T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=0,
    )
    second = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    repository.stage_following_items(second, [_blogger("1", "甲", 0)])
    repository.commit_following_snapshot(
        second, cutoff_at="2026-07-18T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=0, blogger_unconfirmed=True,
    )
    third = repository.begin_following_snapshot("2026-07-19T00:00:00+00:00")
    source = Source(
        [BloggerPage([_blogger("1", "甲", 0), _blogger("2", "乙", 1)], 2, 0, False)],
        FollowingListResult([], 0, True),
    )

    result = FollowingArchiveSync(
        repository,
        source,
        now=lambda: datetime(2026, 7, 19, 1, tzinfo=timezone.utc),
    ).run(_checkpoint(third))

    assert result.unfollowed_count == 0
    assert result.unconfirmed_count == 0
    assert repository.list_following_changes() == []
    relationships = {
        item.object_id: item for item in repository.list_following_relationships()
    }
    assert relationships["2"].ended_snapshot_id is None
    assert relationships["2"].last_confirmed_at == "2026-07-19T01:00:00+00:00"
    repository.close()


def test_more_items_than_reported_total_is_rejected_even_with_filtered_flag(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    source = Source(
        [BloggerPage([_blogger("1", "甲", 0), _blogger("2", "乙", 1)], 1, 0, True)],
        FollowingListResult([_topic("1022:1", "超话甲", 0)], 1, True),
    )

    with pytest.raises(WeiboError, match="报告总数"):
        FollowingArchiveSync(repository, source).run(_checkpoint(snapshot_id))

    assert repository.get_following_snapshot(snapshot_id).status == "staging"
    assert repository.get_current_following_snapshot() is None
    repository.close()


def test_matching_count_with_filtered_flag_stays_strict(tmp_path):
    from weibo_book.archive.following_sync import FollowingArchiveSync

    repository = _repository(tmp_path)
    old = repository.begin_following_snapshot("2026-07-17T00:00:00+00:00")
    repository.stage_following_items(old, [_blogger("1", "甲", 0), _blogger("2", "乙", 1)])
    repository.commit_following_snapshot(
        old, cutoff_at="2026-07-17T01:00:00+00:00", bloggers_complete=True,
        supertopics_complete=True, blogger_reported_total=2,
        supertopic_reported_total=0,
    )
    current = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    source = Source(
        # 条目数与报告总数一致：即使过滤标志为 true，缺失的「乙」也按取消关注处理。
        [BloggerPage([_blogger("1", "甲", 0)], 1, 0, True)],
        FollowingListResult([], 0, True),
    )

    result = FollowingArchiveSync(
        repository,
        source,
        now=lambda: datetime(2026, 7, 18, 1, tzinfo=timezone.utc),
    ).run(_checkpoint(current))

    assert result.unconfirmed_count == 0
    assert result.unfollowed_count == 1
    changes = repository.list_following_changes()
    assert [(item.change_type, item.object_id) for item in changes] == [
        ("unfollowed", "2")
    ]
    repository.close()


def test_completed_snapshot_resume_is_idempotent_and_duration_is_local_only(tmp_path):
    from weibo_book.archive.following_sync import (
        FollowingArchiveSync,
        check_following_duration,
    )

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-18T00:00:00+00:00")
    repository.stage_following_items(snapshot_id, [_blogger("1", "甲", 0), _topic("1022:1", "超话甲", 0)])
    repository.commit_following_snapshot(
        snapshot_id, cutoff_at="2026-07-18T01:00:00+00:00",
        bloggers_complete=True, supertopics_complete=True,
        blogger_reported_total=1, supertopic_reported_total=1,
    )
    checkpoint = _checkpoint(snapshot_id) | {
        "blogger_next_cursor": 0,
        "blogger_completed_count": 1,
        "bloggers_done": True,
        "supertopics_done": True,
        "blogger_reported_total": 1,
        "supertopic_reported_total": 1,
    }
    source = Source([], FollowingListResult([], 0, True))

    result = FollowingArchiveSync(repository, source).run(checkpoint)
    duration = check_following_duration(repository, "blogger", "1")

    assert result.snapshot_id == snapshot_id
    assert source.calls == []
    assert duration.source == "local_minimum"
    assert duration.platform_followed_at == ""
    assert duration.local_first_seen_at == "2026-07-18T01:00:00+00:00"
    with pytest.raises(WeiboError, match="未记录"):
        check_following_duration(repository, "blogger", "2")
    repository.close()
