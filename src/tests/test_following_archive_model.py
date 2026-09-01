import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "following_archive"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _repository(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository

    return ArchiveRepository.create(tmp_path / "archive", "10001", "测试用户")


def _object(object_type="blogger", object_id="1000000001", **changes):
    from weibo_book.archive.following import FollowingObjectRecord

    values = {
        "object_type": object_type,
        "object_id": object_id,
        "display_name": "测试博主" if object_type == "blogger" else "测试超话",
        "page_url": (
            f"https://weibo.com/u/{object_id}"
            if object_type == "blogger"
            else "//weibo.com/p/10080800000000000000000000000000000000"
        ),
        "app_scheme": (
            ""
            if object_type == "blogger"
            else "sinaweibo://pageinfo?containerid=10080800000000000000000000000000000000"
        ),
        "source_order": 0,
    }
    values.update(changes)
    return FollowingObjectRecord(**values)


def _commit(repository, items, cutoff="2026-07-18T00:00:00+00:00"):
    snapshot_id = repository.begin_following_snapshot(
        started_at="2026-07-17T23:00:00+00:00"
    )
    repository.stage_following_items(snapshot_id, items)
    return repository.commit_following_snapshot(
        snapshot_id,
        cutoff_at=cutoff,
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=sum(item.object_type == "blogger" for item in items),
        supertopic_reported_total=sum(
            item.object_type == "supertopic" for item in items
        ),
    )


def test_records_use_fixture_verified_stable_identities():
    from weibo_book.archive.following import FollowingObjectRecord

    user = _fixture("following_page_1.json")["response"]["data"]["follows"][
        "users"
    ][0]
    topic = _fixture("followed_supertopics_page_1.json")["response"]["data"][
        "list"
    ][0]

    blogger = FollowingObjectRecord(
        object_type="blogger",
        object_id=user["idstr"],
        display_name=user["screen_name"],
        page_url=f"https://weibo.com/u/{user['idstr']}",
        app_scheme="",
        source_order=0,
    )
    supertopic = FollowingObjectRecord(
        object_type="supertopic",
        object_id=topic["oid"],
        display_name=topic["topic_name"],
        page_url=topic["link"],
        app_scheme=topic["scheme"],
        source_order=0,
    )

    assert blogger.object_id == "1000000001"
    assert blogger.page_url == "https://weibo.com/u/1000000001"
    assert supertopic.object_id == "1022:10080800000000000000000000000000000000"
    assert supertopic.page_url == "//weibo.com/p/10080800000000000000000000000000000000"
    assert supertopic.app_scheme.startswith("sinaweibo://pageinfo?")


@pytest.mark.parametrize(
    "changes",
    [
        {"object_type": "其他"},
        {"object_id": ""},
        {"object_id": 1000000001},
        {"display_name": ""},
        {"display_name": 1},
        {"source_order": -1},
        {"source_order": "0"},
    ],
)
def test_following_object_record_rejects_invalid_local_identity(changes):
    from weibo_book.archive.following import FollowingObjectRecord

    values = {
        "object_type": "blogger",
        "object_id": "1000000001",
        "display_name": "测试博主",
        "page_url": "https://weibo.com/u/1000000001",
        "app_scheme": "",
        "source_order": 0,
    }
    values.update(changes)

    with pytest.raises(ValueError):
        FollowingObjectRecord(**values)


def test_incomplete_snapshot_cannot_change_formal_current_state(tmp_path):
    from weibo_book.archive.repository import ArchiveError

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-17T23:00:00+00:00")
    repository.stage_following_items(snapshot_id, [_object()])

    with pytest.raises(ArchiveError, match="两类清单均完整"):
        repository.commit_following_snapshot(
            snapshot_id,
            cutoff_at="2026-07-18T00:00:00+00:00",
            bloggers_complete=True,
            supertopics_complete=False,
            blogger_reported_total=1,
            supertopic_reported_total=None,
        )

    assert repository.get_current_following_snapshot() is None
    assert repository._connection.execute(
        "SELECT status FROM following_snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone() == ("staging",)
    assert repository._connection.execute("SELECT * FROM following_objects").fetchall() == []
    repository.close()


def test_reported_total_mismatch_cannot_commit_snapshot(tmp_path):
    from weibo_book.archive.repository import ArchiveError

    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-17T23:00:00+00:00")
    repository.stage_following_items(snapshot_id, [_object()])

    with pytest.raises(ArchiveError, match="报告总数"):
        repository.commit_following_snapshot(
            snapshot_id,
            cutoff_at="2026-07-18T00:00:00+00:00",
            bloggers_complete=True,
            supertopics_complete=True,
            blogger_reported_total=2,
            supertopic_reported_total=0,
        )

    assert repository.get_current_following_snapshot() is None
    repository.close()


def test_discard_staging_snapshot_removes_only_temporary_rows(tmp_path):
    repository = _repository(tmp_path)
    snapshot_id = repository.begin_following_snapshot("2026-07-17T23:00:00+00:00")
    repository.stage_following_items(snapshot_id, [_object()])

    repository.discard_following_snapshot(snapshot_id)

    assert repository._connection.execute("SELECT * FROM following_snapshots").fetchall() == []
    assert repository._connection.execute(
        "SELECT * FROM following_snapshot_items"
    ).fetchall() == []
    repository.close()


def test_first_complete_snapshot_creates_formal_local_history(tmp_path):
    repository = _repository(tmp_path)
    items = [
        _object(),
        _object(
            object_type="supertopic",
            object_id="1022:10080800000000000000000000000000000000",
            source_order=0,
        ),
    ]

    result = _commit(repository, items)

    assert result.initial is True
    assert (result.blogger_count, result.supertopic_count) == (1, 1)
    current = repository.get_current_following_snapshot()
    assert current is not None
    assert current.snapshot_id == result.snapshot_id
    assert current.status == "complete"
    assert current.cutoff_at == "2026-07-18T00:00:00+00:00"
    assert repository.list_following_snapshot_items() == items
    relationships = repository.list_following_relationships()
    assert len(relationships) == 2
    assert all(item.ended_snapshot_id is None for item in relationships)
    assert all(
        item.local_first_seen_at == "2026-07-18T00:00:00+00:00"
        for item in relationships
    )
    assert all(item.platform_followed_at == "" for item in relationships)
    assert len(repository.list_following_names()) == 2
    assert repository.list_following_changes() == []
    repository.close()


def test_complete_empty_snapshot_is_distinct_from_incomplete_result(tmp_path):
    repository = _repository(tmp_path)

    result = _commit(repository, [])

    assert result.initial is True
    assert (result.blogger_count, result.supertopic_count) == (0, 0)
    assert repository.get_current_following_snapshot().snapshot_id == result.snapshot_id
    assert repository.list_following_snapshot_items() == []
    repository.close()


def test_migrated_archive_without_following_data_has_empty_read_model(tmp_path):
    repository = _repository(tmp_path)

    assert repository.get_current_following_snapshot() is None
    assert repository.list_following_snapshot_items() == []
    assert repository.list_following_relationships() == []
    assert repository.list_following_names() == []
    assert repository.list_following_changes() == []
    repository.close()


def test_complete_snapshot_closes_missing_relationship_and_keeps_present_one(tmp_path):
    repository = _repository(tmp_path)
    blogger = _object()
    topic = _object(
        object_type="supertopic",
        object_id="1022:10080800000000000000000000000000000000",
    )
    _commit(repository, [blogger, topic])

    second = _commit(
        repository,
        [blogger],
        cutoff="2026-07-19T00:00:00+00:00",
    )

    relationships = repository.list_following_relationships()
    blogger_relationship = next(
        item for item in relationships if item.object_type == "blogger"
    )
    topic_relationship = next(
        item for item in relationships if item.object_type == "supertopic"
    )
    assert blogger_relationship.ended_snapshot_id is None
    assert blogger_relationship.last_confirmed_at == "2026-07-19T00:00:00+00:00"
    assert topic_relationship.ended_snapshot_id == second.snapshot_id
    changes = repository.list_following_changes()
    assert [(item.change_type, item.object_id) for item in changes] == [
        ("unfollowed", topic.object_id)
    ]

    third = _commit(repository, [], cutoff="2026-07-20T00:00:00+00:00")
    assert repository.list_following_snapshot_items() == []
    assert all(
        item.ended_snapshot_id is not None
        for item in repository.list_following_relationships()
    )
    assert third.unfollowed_count == 1
    repository.close()


def test_reappearing_object_creates_new_relationship_period_and_refollowed_change(
    tmp_path,
):
    repository = _repository(tmp_path)
    blogger = _object()
    _commit(repository, [blogger])
    _commit(repository, [], cutoff="2026-07-19T00:00:00+00:00")

    third = _commit(repository, [blogger], cutoff="2026-07-20T00:00:00+00:00")

    relationships = repository.list_following_relationships()
    assert len(relationships) == 2
    assert relationships[0].ended_snapshot_id is not None
    assert relationships[1].started_snapshot_id == third.snapshot_id
    assert relationships[1].ended_snapshot_id is None
    assert [item.change_type for item in repository.list_following_changes()] == [
        "unfollowed",
        "refollowed",
    ]
    repository.close()


def test_name_change_is_recorded_only_after_complete_snapshot(tmp_path):
    repository = _repository(tmp_path)
    blogger = _object(display_name="旧名称")
    _commit(repository, [blogger])
    snapshot_id = repository.begin_following_snapshot("2026-07-18T23:00:00+00:00")
    repository.stage_following_items(
        snapshot_id,
        [_object(display_name="新名称")],
    )

    assert repository._connection.execute(
        "SELECT current_name FROM following_objects WHERE object_type='blogger'"
    ).fetchone() == ("旧名称",)

    repository.commit_following_snapshot(
        snapshot_id,
        cutoff_at="2026-07-19T00:00:00+00:00",
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=1,
        supertopic_reported_total=0,
    )

    names = repository.list_following_names()
    assert [(item.name, item.ended_snapshot_id is None) for item in names] == [
        ("旧名称", False),
        ("新名称", True),
    ]
    changes = repository.list_following_changes()
    assert len(changes) == 1
    assert changes[0].change_type == "renamed"
    assert changes[0].before == {"name": "旧名称"}
    assert changes[0].after == {"name": "新名称"}
    repository.close()


def test_new_object_and_platform_followed_at_keep_distinct_time_sources(tmp_path):
    repository = _repository(tmp_path)
    _commit(repository, [])
    blogger = _object(platform_followed_at="2024-01-02T03:04:05+08:00")

    second = _commit(repository, [blogger], cutoff="2026-07-19T00:00:00+00:00")

    relationship = repository.list_following_relationships()[0]
    assert relationship.local_first_seen_at == "2026-07-19T00:00:00+00:00"
    assert relationship.platform_followed_at == "2024-01-02T03:04:05+08:00"
    changes = repository.list_following_changes()
    assert [(item.snapshot_id, item.change_type) for item in changes] == [
        (second.snapshot_id, "followed")
    ]
    repository.close()


def test_commit_failure_rolls_back_all_formal_following_state(tmp_path):
    from weibo_book.archive.repository import ArchiveError

    repository = _repository(tmp_path)
    first = _commit(repository, [_object(display_name="旧名称")])
    snapshot_id = repository.begin_following_snapshot("2026-07-18T23:00:00+00:00")
    repository.stage_following_items(
        snapshot_id,
        [_object(display_name="新名称")],
    )
    repository._connection.execute(
        """
        CREATE TRIGGER fail_following_pointer_update
        BEFORE UPDATE ON following_state
        BEGIN
            SELECT RAISE(ABORT,'模拟当前指针写入失败');
        END
        """
    )

    with pytest.raises(ArchiveError, match="归档数据库操作失败"):
        repository.commit_following_snapshot(
            snapshot_id,
            cutoff_at="2026-07-19T00:00:00+00:00",
            bloggers_complete=True,
            supertopics_complete=True,
            blogger_reported_total=1,
            supertopic_reported_total=0,
        )

    assert repository.get_current_following_snapshot().snapshot_id == first.snapshot_id
    assert repository._connection.execute(
        "SELECT status FROM following_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone() == ("staging",)
    assert [(item.name, item.ended_snapshot_id) for item in repository.list_following_names()] == [
        ("旧名称", None)
    ]
    assert repository.list_following_changes() == []
    repository.close()


def test_unconfirmed_commit_keeps_missing_blogger_relationship_open(tmp_path):
    repository = _repository(tmp_path)
    _commit(
        repository,
        [_object(object_id="1"), _object(object_id="2", source_order=1)],
    )
    snapshot_id = repository.begin_following_snapshot("2026-07-19T00:00:00+00:00")
    repository.stage_following_items(snapshot_id, [_object(object_id="1")])

    result = repository.commit_following_snapshot(
        snapshot_id,
        cutoff_at="2026-07-19T01:00:00+00:00",
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=2,
        supertopic_reported_total=0,
        blogger_unconfirmed=True,
    )

    assert result.unconfirmed_count == 1
    assert result.unfollowed_count == 0
    relationships = repository.list_following_relationships()
    assert all(item.ended_snapshot_id is None for item in relationships)
    snapshot = repository.get_following_snapshot(snapshot_id)
    assert snapshot.summary["blogger_unconfirmed"] is True
    assert snapshot.summary["unconfirmed_bloggers"] == [
        {
            "object_id": "2",
            "name": "测试博主",
            "page_url": "https://weibo.com/u/2",
        }
    ]
    assert repository.list_following_changes() == []
    repository.close()


def test_unconfirmed_commit_still_rejects_more_items_than_reported(tmp_path):
    from weibo_book.archive.repository import ArchiveError

    repository = _repository(tmp_path)
    _commit(repository, [_object(object_id="1")])
    snapshot_id = repository.begin_following_snapshot("2026-07-19T00:00:00+00:00")
    repository.stage_following_items(
        snapshot_id,
        [_object(object_id="1"), _object(object_id="2", source_order=1)],
    )

    with pytest.raises(ArchiveError, match="报告总数"):
        repository.commit_following_snapshot(
            snapshot_id,
            cutoff_at="2026-07-19T01:00:00+00:00",
            bloggers_complete=True,
            supertopics_complete=True,
            blogger_reported_total=1,
            supertopic_reported_total=0,
            blogger_unconfirmed=True,
        )

    assert repository.get_following_snapshot(snapshot_id).status == "staging"
    repository.close()
