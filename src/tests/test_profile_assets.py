"""schema v3/v4 的资料媒体（头像/封面）、置顶观察与粉丝数观察记录测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weibo_book.archive.discovery import ProfileItem, ProfilePage
from weibo_book.archive.repository import ArchiveError, ArchiveRepository
from weibo_book.archive.schema import (
    MediaRecord,
    PostRecord,
    ProfileAssetRecord,
    read_schema_version,
)
from weibo_book.errors import WeiboError
from weibo_book.models import Comment, MediaType, Post, PostMedia


_V2_V3_TABLES = (
    "following_state",
    "following_changes",
    "following_names",
    "following_relationships",
    "following_objects",
    "following_snapshot_items",
    "following_snapshots",
    "profile_assets",
    "pin_observations",
)

_SEED_TIME = "2026-08-01T00:00:00+00:00"


def _post_record(bid: str, *, is_pinned: bool = False, pin_order: int | None = None):
    return PostRecord(
        bid=bid,
        uid="10001",
        text="微博正文",
        is_pinned=is_pinned,
        pin_order=pin_order,
    )


def _avatar_media(url: str = "https://example.test/avatar.jpg"):
    return MediaRecord(
        "user", "10001", "avatar", 0, url, "媒体/头像/avatar.jpg", "ab" * 32
    )


def _create_archive_with_history(tmp_path: Path) -> ArchiveRepository:
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    repository.upsert_post(_post_record("P001", is_pinned=True, pin_order=1))
    repository.upsert_post(_post_record("P002"))
    repository.upsert_media(_avatar_media())
    return repository


def _rewrite_manifest(root: Path, **changes) -> None:
    manifest_path = root / "数据与媒体" / "数据" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(changes)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def _demote_to_version(repository: ArchiveRepository, version: int) -> None:
    if version == 1:
        tables = _V2_V3_TABLES + ("fans_observations",)
    elif version == 2:
        tables = ("profile_assets", "pin_observations", "fans_observations")
    else:
        tables = ("fans_observations",)
    for table in tables:
        repository._connection.execute(f"DROP TABLE {table}")
    repository._connection.execute(
        "UPDATE archive_meta SET value_json = ? WHERE key = 'schema_version'",
        (str(version),),
    )
    repository._connection.commit()


def test_fresh_archive_is_schema_v4_and_validates(tmp_path):
    from weibo_book.archive.schema import SCHEMA_VERSION, validate_schema

    repository = _create_archive_with_history(tmp_path)
    assert SCHEMA_VERSION == 4
    assert read_schema_version(repository._connection) == 4
    validate_schema(repository._connection)
    repository.close()


@pytest.mark.parametrize("source_version", [1, 2])
def test_migration_seeds_pin_observations_and_profile_assets(tmp_path, source_version):
    repository = _create_archive_with_history(tmp_path)
    root = repository._root
    _demote_to_version(repository, source_version)
    repository.close()
    _rewrite_manifest(
        root, schema_version=source_version, last_successful_sync_at=_SEED_TIME
    )

    migrated = ArchiveRepository.open(root, expected_uid="10001")

    assert read_schema_version(migrated._connection) == 4
    assert migrated.manifest().schema_version == 4
    observations = migrated.list_pin_observations()
    assert [
        (item.run_id, item.observed_at, item.bid, item.pin_order)
        for item in observations
    ] == [("legacy-seed", _SEED_TIME, "P001", 1)]
    assets = migrated.list_profile_assets()
    assert assets == [
        ProfileAssetRecord(
            "avatar",
            "https://example.test/avatar.jpg",
            "媒体/头像/avatar.jpg",
            "ab" * 32,
            _SEED_TIME,
            "legacy-seed",
        )
    ]
    assert json.loads(
        root.joinpath("数据与媒体", "数据", "manifest.json").read_text(encoding="utf-8")
    )["schema_version"] == 4
    migrated.close()


def test_migration_from_v3_adds_fans_observations_without_seeding(tmp_path):
    repository = _create_archive_with_history(tmp_path)
    root = repository._root
    _demote_to_version(repository, 3)
    repository.close()
    _rewrite_manifest(root, schema_version=3, last_successful_sync_at=_SEED_TIME)

    migrated = ArchiveRepository.open(root, expected_uid="10001")

    assert read_schema_version(migrated._connection) == 4
    assert migrated.manifest().schema_version == 4
    # v3 库已有置顶观察表，迁移不再播种 legacy 行
    assert migrated.list_pin_observations() == []
    assert migrated.list_fans_observations() == []
    migrated.record_fans_observation("run-1", _SEED_TIME, 100)
    assert [item.followers_count for item in migrated.list_fans_observations()] == [100]
    migrated.close()


def test_migration_seed_prefers_archive_meta_timestamp(tmp_path):
    repository = _create_archive_with_history(tmp_path)
    root = repository._root
    meta_time = "2026-06-01T12:00:00+00:00"
    repository._connection.execute(
        "INSERT INTO archive_meta(key, value_json) VALUES ('last_successful_sync_at', ?)",
        (json.dumps(meta_time),),
    )
    _demote_to_version(repository, 2)
    repository.close()
    _rewrite_manifest(root, schema_version=2, last_successful_sync_at=_SEED_TIME)

    migrated = ArchiveRepository.open(root, expected_uid="10001")

    assert [item.observed_at for item in migrated.list_pin_observations()] == [meta_time]
    assert [item.first_seen_at for item in migrated.list_profile_assets()] == [meta_time]
    migrated.close()


def test_migration_seed_falls_back_to_current_time(tmp_path):
    repository = _create_archive_with_history(tmp_path)
    root = repository._root
    _demote_to_version(repository, 2)
    repository.close()
    _rewrite_manifest(root, schema_version=2, last_successful_sync_at="")

    migrated = ArchiveRepository.open(root, expected_uid="10001")

    observed_at = migrated.list_pin_observations()[0].observed_at
    datetime.fromisoformat(observed_at)
    migrated.close()


def test_seeded_rows_stay_unique_when_recorded_again(tmp_path):
    repository = _create_archive_with_history(tmp_path)
    root = repository._root
    _demote_to_version(repository, 2)
    repository.close()
    _rewrite_manifest(root, schema_version=2, last_successful_sync_at=_SEED_TIME)
    migrated = ArchiveRepository.open(root, expected_uid="10001")

    migrated.record_pin_observations("legacy-seed", _SEED_TIME, [("P001", 1)])
    migrated.record_pin_observations("legacy-seed", _SEED_TIME, [("P001", 1)])
    assert len(migrated.list_pin_observations()) == 1
    assert migrated.record_profile_asset(
        "avatar", "https://example.test/avatar.jpg",
        first_seen_at=_SEED_TIME, run_id="legacy-seed",
    ) is False
    assert len(migrated.list_profile_assets()) == 1
    migrated.close()


def test_record_profile_asset_insert_and_completion_update(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )

    assert repository.record_profile_asset(
        "cover", "https://example.test/cover.jpg",
        first_seen_at="2026-09-07T00:00:00+00:00", run_id="run-1",
    ) is True
    # 原 local_path 为空且本次提供本地文件时补全
    assert repository.record_profile_asset(
        "cover", "https://example.test/cover.jpg",
        local_path="media/covers/other.jpg", sha256="ff" * 32,
        first_seen_at="2026-09-07T01:00:00+00:00", run_id="run-2",
    ) is False
    assert repository.list_profile_assets("cover")[0].local_path == "media/covers/other.jpg"
    # 已有 local_path 时不再覆盖
    repository.record_profile_asset(
        "cover", "https://example.test/cover.jpg",
        local_path="media/covers/cover.jpg", sha256="cd" * 32,
        first_seen_at="2026-09-07T02:00:00+00:00", run_id="run-3",
    )
    stored = repository.list_profile_assets("cover")[0]
    assert stored.local_path == "media/covers/other.jpg"
    assert stored.sha256 == "ff" * 32
    assert stored.first_seen_at == "2026-09-07T00:00:00+00:00"
    repository.close()


def test_record_profile_asset_rejects_invalid_input(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    with pytest.raises(ArchiveError, match="资料媒体类型无效"):
        repository.record_profile_asset(
            "banner", "https://example.test/x.jpg",
            first_seen_at="2026-09-07T00:00:00+00:00", run_id="run-1",
        )
    with pytest.raises(ArchiveError, match="资料媒体链接不能为空"):
        repository.record_profile_asset(
            "avatar", "", first_seen_at="2026-09-07T00:00:00+00:00", run_id="run-1",
        )
    with pytest.raises(ArchiveError, match="POSIX"):
        repository.record_profile_asset(
            "avatar", "https://example.test/x.jpg", local_path="avatars/x.jpg",
            first_seen_at="2026-09-07T00:00:00+00:00", run_id="run-1",
        )
    with pytest.raises(ArchiveError, match="资料媒体类型无效"):
        repository.list_profile_assets("banner")
    repository.close()


def test_list_profile_assets_orders_by_first_seen_desc(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    repository.record_profile_asset(
        "avatar", "https://example.test/old.jpg",
        first_seen_at="2026-01-01T00:00:00+00:00", run_id="run-1",
    )
    repository.record_profile_asset(
        "avatar", "https://example.test/new.jpg",
        first_seen_at="2026-09-01T00:00:00+00:00", run_id="run-2",
    )
    repository.record_profile_asset(
        "cover", "https://example.test/cover.jpg",
        first_seen_at="2026-06-01T00:00:00+00:00", run_id="run-3",
    )

    assert [item.remote_url for item in repository.list_profile_assets("avatar")] == [
        "https://example.test/new.jpg",
        "https://example.test/old.jpg",
    ]
    assert [item.kind for item in repository.list_profile_assets()] == [
        "avatar", "cover", "avatar",
    ]
    repository.close()


def test_pin_observation_batch_listing_and_latest_batch(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    repository.record_pin_observations(
        "run-1", "2026-08-01T00:00:00+00:00", [("A", 1), ("B", 2)]
    )
    repository.record_pin_observations(
        "run-2", "2026-09-01T00:00:00+00:00", [("C", 1)]
    )

    assert [
        (item.run_id, item.bid, item.pin_order)
        for item in repository.list_pin_observations()
    ] == [("run-2", "C", 1), ("run-1", "A", 1), ("run-1", "B", 2)]
    assert repository.latest_pin_bids() == {"C"}
    repository.close()


def test_pin_observation_rejects_invalid_items(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    with pytest.raises(ArchiveError, match="置顶观察条目无效"):
        repository.record_pin_observations(
            "run-1", "2026-09-01T00:00:00+00:00", [("", 1)]
        )
    with pytest.raises(ArchiveError, match="置顶观察条目无效"):
        repository.record_pin_observations(
            "run-1", "2026-09-01T00:00:00+00:00", [("A", "1")]
        )
    with pytest.raises(ArchiveError, match="置顶观察时间不能为空"):
        repository.record_pin_observations("run-1", "", [("A", 1)])
    repository.close()


def test_fans_observation_recording_listing_and_idempotency(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    repository.record_fans_observation("run-1", "2026-08-01T00:00:00+00:00", 1200)
    repository.record_fans_observation("run-2", "2026-09-01T00:00:00+00:00", 1350)
    repository.record_fans_observation("run-2", "2026-09-01T00:00:00+00:00", 9999)

    assert [
        (item.run_id, item.followers_count)
        for item in repository.list_fans_observations()
    ] == [("run-2", 1350), ("run-1", 1200)]
    repository.close()


def test_fans_observation_rejects_invalid_values(tmp_path):
    repository = ArchiveRepository.create(
        tmp_path / "archive", uid="10001", screen_name="测试用户"
    )
    with pytest.raises(ArchiveError, match="粉丝数观察运行标识不能为空"):
        repository.record_fans_observation("", "2026-09-01T00:00:00+00:00", 1)
    with pytest.raises(ArchiveError, match="粉丝数观察时间不能为空"):
        repository.record_fans_observation("run-1", "", 1)
    with pytest.raises(ArchiveError, match="粉丝数观察数值无效"):
        repository.record_fans_observation("run-1", "2026-09-01T00:00:00+00:00", -1)
    with pytest.raises(ArchiveError, match="粉丝数观察数值无效"):
        repository.record_fans_observation("run-1", "2026-09-01T00:00:00+00:00", True)
    repository.close()


class _IdentityProvider:
    def whoami(self) -> dict:
        return {"uid": "10001", "screen_name": "测试用户"}


class _PinnedSource:
    def __init__(self, pages, posts):
        self._pages = pages
        self._posts = posts

    def iter_profile_pages(self, uid: str):
        yield from self._pages

    def fetch_post(self, uid: str, bid: str) -> Post:
        return self._posts[bid]

    def fetch_recent_comments(self, post_id: str, limit: int = 10) -> list[Comment]:
        return []


class _ProfileAssetSource(_PinnedSource):
    def fetch_profile_assets(self, uid: str):
        return (
            "https://example.test/avatar.jpg",
            "https://example.test/cover.jpg",
            12345,
        )


def _sync_post(bid: str) -> Post:
    return Post(
        bid=bid,
        uid="10001",
        user_name="测试用户",
        user_avatar="",
        text="正文",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_successful_sync_records_page_one_pin_observation(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [
        ProfilePage(
            [ProfileItem("A", True, 1), ProfileItem("B", True, 2), ProfileItem("C")],
            is_last=False,
        ),
        ProfilePage([ProfileItem("D", True, 3)], is_last=True),
    ]
    posts = {bid: _sync_post(bid) for bid in ("A", "B", "C", "D")}
    PersonalArchiveSync(root, _PinnedSource(pages, posts), _IdentityProvider()).run("create")

    repository = ArchiveRepository.open(root, "10001")
    run_id, status = repository.get_latest_sync_status("create")
    assert status == "done"
    # 只记录第 1 页观察到的置顶（D 在第 2 页，不属于本次第 1 页观察）
    assert [
        (item.run_id, item.bid, item.pin_order)
        for item in repository.list_pin_observations()
    ] == [(run_id, "A", 1), (run_id, "B", 2)]
    assert repository.latest_pin_bids() == {"A", "B"}
    repository.close()


def test_sync_without_profile_asset_support_records_nothing(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    PersonalArchiveSync(
        root, _PinnedSource(pages, {"A": _sync_post("A")}), _IdentityProvider()
    ).run("create")

    repository = ArchiveRepository.open(root, "10001")
    assert repository.list_profile_assets() == []
    assert repository.list_pin_observations() == []
    repository.close()


def test_sync_records_profile_assets_and_downloads_new_files(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]

    def fake_download(self, paths, kind, url):
        directory = paths.media_root / ("头像" if kind == "avatar" else "封面")
        directory.mkdir(parents=True, exist_ok=True)
        content = f"{kind} 内容".encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        target = directory / f"{digest[:16]}.jpg"
        target.write_bytes(content)
        return f"媒体/{directory.name}/{target.name}", digest

    monkeypatch.setattr(PersonalArchiveSync, "_download_profile_asset", fake_download)
    PersonalArchiveSync(
        root, _ProfileAssetSource(pages, {"A": _sync_post("A")}), _IdentityProvider()
    ).run("create")

    repository = ArchiveRepository.open(root, "10001")
    assets = repository.list_profile_assets()
    # 同一批同步的 first_seen_at 相同，按 id 倒序，封面后插入排在前面
    assert [(item.kind, item.remote_url) for item in assets] == [
        ("cover", "https://example.test/cover.jpg"),
        ("avatar", "https://example.test/avatar.jpg"),
    ]
    for item in assets:
        assert item.local_path.startswith(("媒体/头像/", "媒体/封面/"))
        assert len(item.sha256) == 64
        assert repository.paths.media_file_path(item.local_path).is_file()
    fans = repository.list_fans_observations()
    assert len(fans) == 1
    assert repository.get_current_profile_asset("avatar") == "https://example.test/avatar.jpg"
    assert repository.get_current_profile_asset("cover") == "https://example.test/cover.jpg"
    assert fans[0].followers_count == 12345
    repository.close()


def test_cover_download_failure_does_not_block_sync(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A", True, 1)], is_last=True)]

    def failing_download(self, archive_root, kind, url):
        raise WeiboError("模拟下载失败")

    monkeypatch.setattr(
        PersonalArchiveSync, "_download_profile_asset", failing_download
    )
    result = PersonalArchiveSync(
        root, _ProfileAssetSource(pages, {"A": _sync_post("A")}), _IdentityProvider()
    ).run("create")

    assert result.new_posts == 1
    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("create")[1] == "done"
    # 下载失败仍记录链接，local_path 留空
    assets = repository.list_profile_assets()
    assert len(assets) == 2
    assert all(item.local_path == "" and item.sha256 == "" for item in assets)
    assert repository.latest_pin_bids() == {"A"}
    repository.close()


def test_sync_retries_profile_asset_download_after_failure(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    source = _ProfileAssetSource(pages, {"A": _sync_post("A")})

    def failing_download(self, archive_root, kind, url):
        raise WeiboError("模拟下载失败")

    monkeypatch.setattr(
        PersonalArchiveSync, "_download_profile_asset", failing_download
    )
    PersonalArchiveSync(root, source, _IdentityProvider()).run("create")
    repository = ArchiveRepository.open(root, "10001")
    assert all(item.local_path == "" for item in repository.list_profile_assets())
    repository.close()

    def recovered_download(self, paths, kind, url):
        directory = paths.media_root / ("头像" if kind == "avatar" else "封面")
        directory.mkdir(parents=True, exist_ok=True)
        content = f"{kind} 内容".encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        target = directory / f"{digest[:16]}.jpg"
        target.write_bytes(content)
        return f"媒体/{directory.name}/{target.name}", digest

    monkeypatch.setattr(
        PersonalArchiveSync, "_download_profile_asset", recovered_download
    )
    PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")

    repository = ArchiveRepository.open(root, "10001")
    assets = repository.list_profile_assets()
    assert len(assets) == 2
    for item in assets:
        assert len(item.sha256) == 64
        assert repository.paths.media_file_path(item.local_path).is_file()
    repository.close()


def test_cleanup_keeps_profile_asset_files(tmp_path):
    from weibo_book.archive.media_cleanup import cleanup_unreferenced_media

    from weibo_book.archive.paths import ArchivePaths

    root = tmp_path / "archive"
    paths = ArchivePaths(root, 1)
    covers = root / "media" / "covers"
    covers.mkdir(parents=True)
    (covers / "cover.jpg").write_bytes(b"cover")
    avatars = root / "media" / "avatars"
    avatars.mkdir(parents=True)
    (avatars / "history.jpg").write_bytes(b"avatar")

    # 没有白名单时，不在 media 表里的文件会被清理
    assert cleanup_unreferenced_media(paths, []) == [
        "media/avatars/history.jpg",
        "media/covers/cover.jpg",
    ]
    (covers / "cover.jpg").write_bytes(b"cover")
    (avatars / "history.jpg").write_bytes(b"avatar")
    assert cleanup_unreferenced_media(
        paths, [], ["media/covers/cover.jpg", "media/avatars/history.jpg", ""]
    ) == []
    assert (covers / "cover.jpg").is_file()
    assert (avatars / "history.jpg").is_file()


# ===== 头像相册（历史头像补抓） =====

_ALBUM_LIST_PAYLOAD = {
    "result": True,
    "code": 0,
    "data": {
        "total": 3,
        "album_list": [
            {"album_id": "111", "caption": "微博配图", "type": 3},
            {"album_id": "222", "caption": "头像相册", "type": 18},
            {"album_id": "333", "caption": "默认专辑", "type": 24},
        ],
    },
}

_PHOTO_PAGE_PAYLOAD = {
    "result": True,
    "code": 0,
    "data": {
        "total": 2,
        "photo_list": [
            {
                "photo_id": "1",
                "pic_host": "https://wx1.sinaimg.cn",
                "pic_name": "aaa.jpg",
                "timestamp": 1587561711,
            },
            {
                "photo_id": "2",
                "pic_host": "https://tvax1.sinaimg.cn",
                "pic_name": "bbb.jpg",
                "timestamp": 1697225395,
            },
        ],
    },
}


def test_parse_avatar_album_id_finds_caption_match():
    from weibo_book.archive.avatar_album import parse_avatar_album_id

    assert parse_avatar_album_id(_ALBUM_LIST_PAYLOAD) == "222"
    assert parse_avatar_album_id({"result": True, "data": {"album_list": []}}) is None
    assert parse_avatar_album_id({"result": False, "data": {}}) is None
    assert parse_avatar_album_id("不是字典") is None
    payload = {
        "result": True,
        "data": {"album_list": [{"album_id": "", "caption": "头像相册"}]},
    }
    assert parse_avatar_album_id(payload) is None


def test_parse_photo_page_builds_large_url_and_iso_time():
    from weibo_book.archive.avatar_album import parse_photo_page

    photos, total = parse_photo_page(_PHOTO_PAGE_PAYLOAD)
    assert total == 2
    assert [url for url, _ in photos] == [
        "https://wx1.sinaimg.cn/large/aaa.jpg",
        "https://tvax1.sinaimg.cn/large/bbb.jpg",
    ]
    assert photos[0][1] == "2020-04-22T13:21:51+00:00"
    assert photos[1][1] == "2023-10-13T19:29:55+00:00"


def test_parse_photo_page_skips_malformed_entries():
    from weibo_book.archive.avatar_album import parse_photo_page

    payload = {
        "result": True,
        "data": {
            "total": 4,
            "photo_list": [
                {"pic_host": "http://insecure.example", "pic_name": "a.jpg", "timestamp": 1},
                {"pic_host": "https://wx1.sinaimg.cn", "pic_name": "", "timestamp": 1},
                {"pic_host": "https://wx1.sinaimg.cn", "pic_name": "a/b.jpg", "timestamp": 1},
                {
                    "pic_host": "https://wx1.sinaimg.cn",
                    "pic_name": "ok.jpg",
                    "timestamp": "不是数字",
                },
            ],
        },
    }
    photos, total = parse_photo_page(payload)
    assert total == 4
    assert photos == [("https://wx1.sinaimg.cn/large/ok.jpg", None)]
    assert parse_photo_page({"result": True, "data": {"total": 0}}) == ([], 0)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, routes):
        self._routes = routes
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        handler = self._routes[url]
        if isinstance(handler, Exception):
            raise handler
        return handler


class _FakeExtractor:
    def __init__(self, session):
        self.client = type("_FakeClient", (), {"session": session})()


def _make_album_source(session):
    from weibo_book.archive.source import WeiboArchiveSource

    return WeiboArchiveSource(
        _FakeExtractor(session),
        self_uid="10001",
        image_quality=None,
    )


def test_fetch_avatar_album_two_requests_and_parsed_photos():
    from weibo_book.archive.avatar_album import ALBUM_LIST_URL, PHOTO_LIST_URL

    session = _FakeSession({
        ALBUM_LIST_URL: _FakeResponse(_ALBUM_LIST_PAYLOAD),
        PHOTO_LIST_URL: _FakeResponse(_PHOTO_PAGE_PAYLOAD),
    })
    source = _make_album_source(session)

    photos = source.fetch_avatar_album("10001")

    assert [url for url, _ in photos] == [
        "https://wx1.sinaimg.cn/large/aaa.jpg",
        "https://tvax1.sinaimg.cn/large/bbb.jpg",
    ]
    assert [url for url, _ in session.requests] == [ALBUM_LIST_URL, PHOTO_LIST_URL]
    assert session.requests[0][1]["uid"] == "10001"
    assert session.requests[1][1]["album_id"] == "222"
    assert session.requests[1][1]["count"] == 100


def test_fetch_avatar_album_without_avatar_album_returns_empty():
    from weibo_book.archive.avatar_album import ALBUM_LIST_URL

    payload = {
        "result": True,
        "data": {"total": 1, "album_list": [{"album_id": "111", "caption": "微博配图"}]},
    }
    session = _FakeSession({ALBUM_LIST_URL: _FakeResponse(payload)})
    source = _make_album_source(session)

    assert source.fetch_avatar_album("10001") == []
    assert len(session.requests) == 1


def test_fetch_avatar_album_paginates_up_to_two_pages():
    from weibo_book.archive.avatar_album import (
        ALBUM_LIST_URL,
        PHOTO_LIST_URL,
        PHOTO_PAGE_SIZE,
    )

    def photo(index):
        return {
            "pic_host": "https://wx1.sinaimg.cn",
            "pic_name": f"p{index}.jpg",
            "timestamp": 1587561711,
        }

    page_one = {
        "result": True,
        "data": {
            "total": PHOTO_PAGE_SIZE * 2,
            "photo_list": [photo(i) for i in range(PHOTO_PAGE_SIZE)],
        },
    }
    page_two = {
        "result": True,
        "data": {
            "total": PHOTO_PAGE_SIZE * 2,
            "photo_list": [photo(i) for i in range(PHOTO_PAGE_SIZE)],
        },
    }
    session = _FakeSession({
        ALBUM_LIST_URL: _FakeResponse(_ALBUM_LIST_PAYLOAD),
        PHOTO_LIST_URL: None,  # 占位，下面按页区分
    })

    def get(url, params=None, timeout=None):
        session.requests.append((url, dict(params or {})))
        if url == ALBUM_LIST_URL:
            return _FakeResponse(_ALBUM_LIST_PAYLOAD)
        return _FakeResponse(page_one if params["page"] == 1 else page_two)

    session.get = get
    source = _make_album_source(session)

    photos = source.fetch_avatar_album("10001")
    assert len(photos) == PHOTO_PAGE_SIZE * 2
    photo_requests = [p for url, p in session.requests if url == PHOTO_LIST_URL]
    assert [p["page"] for p in photo_requests] == [1, 2]


def test_fetch_avatar_album_http_error_raises_weibo_error():
    from weibo_book.archive.avatar_album import ALBUM_LIST_URL

    session = _FakeSession({ALBUM_LIST_URL: _FakeResponse({}, status_code=500)})
    source = _make_album_source(session)

    with pytest.raises(WeiboError, match="读取头像相册列表失败"):
        source.fetch_avatar_album("10001")


def test_fetch_avatar_album_rejects_other_uid():
    session = _FakeSession({})
    source = _make_album_source(session)
    with pytest.raises(WeiboError, match="只允许访问归档目标账号"):
        source.fetch_avatar_album("99999")


class _AlbumBackfillSource(_ProfileAssetSource):
    def __init__(self, pages, posts, album_photos):
        super().__init__(pages, posts)
        self._album_photos = album_photos

    def fetch_avatar_album(self, uid: str):
        if isinstance(self._album_photos, Exception):
            raise self._album_photos
        return self._album_photos


def test_sync_backfills_avatar_album_history(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    album_photos = [
        ("https://wx1.sinaimg.cn/large/old1.jpg", "2020-04-22T13:21:51+00:00"),
        ("https://wx1.sinaimg.cn/large/old2.jpg", None),
        # 与当前头像同 URL：按 (kind, remote_url) 去重，不重复记录
        ("https://example.test/avatar.jpg", "2023-10-13T20:49:55+00:00"),
    ]

    def fake_download(self, paths, kind, url):
        directory = paths.media_root / ("头像" if kind == "avatar" else "封面")
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        target = directory / f"{digest[:16]}.jpg"
        target.write_bytes(url.encode("utf-8"))
        return f"媒体/{directory.name}/{target.name}", digest

    monkeypatch.setattr(PersonalArchiveSync, "_download_profile_asset", fake_download)
    PersonalArchiveSync(
        root,
        _AlbumBackfillSource(pages, {"A": _sync_post("A")}, album_photos),
        _IdentityProvider(),
    ).run("create")

    repository = ArchiveRepository.open(root, "10001")
    avatars = repository.list_profile_assets("avatar")
    by_url = {item.remote_url: item for item in avatars}
    assert set(by_url) == {
        "https://example.test/avatar.jpg",
        "https://wx1.sinaimg.cn/large/old1.jpg",
        "https://wx1.sinaimg.cn/large/old2.jpg",
    }
    # 专辑自带时间作为 first_seen_at；缺失时用本次同步时间
    assert by_url["https://wx1.sinaimg.cn/large/old1.jpg"].first_seen_at == (
        "2020-04-22T13:21:51+00:00"
    )
    assert by_url["https://wx1.sinaimg.cn/large/old2.jpg"].first_seen_at != ""
    for item in avatars:
        assert item.local_path.startswith("媒体/头像/")
        assert len(item.sha256) == 64
    repository.close()


def test_sync_avatar_album_failure_does_not_block(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]

    def fake_download(self, paths, kind, url):
        directory = paths.media_root / ("头像" if kind == "avatar" else "封面")
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        target = directory / f"{digest[:16]}.jpg"
        target.write_bytes(url.encode("utf-8"))
        return f"媒体/{directory.name}/{target.name}", digest

    monkeypatch.setattr(PersonalArchiveSync, "_download_profile_asset", fake_download)
    result = PersonalArchiveSync(
        root,
        _AlbumBackfillSource(
            pages, {"A": _sync_post("A")}, WeiboError("模拟相册接口失败")
        ),
        _IdentityProvider(),
    ).run("create")

    assert result.new_posts == 1
    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("create")[1] == "done"
    # 当前头像/封面照常记录，历史头像为空
    assert len(repository.list_profile_assets()) == 2
    repository.close()


def _video_post(bid: str) -> Post:
    return Post(
        bid=bid,
        uid="10001",
        user_name="测试用户",
        user_avatar="",
        text="视频正文",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        media=[
            PostMedia(
                type=MediaType.VIDEO,
                url=f"https://f.video.weibocdn.com/{bid}.mp4",
            )
        ],
    )


def _seed_missing_video_posts(root: Path, bids: list[str]) -> None:
    """直接写入带视频签名但无本地视频文件的微博，模拟早期版本的失败状态。"""
    from weibo_book.archive.sync import post_to_record

    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")
    for bid in bids:
        repository.upsert_post(post_to_record(_video_post(bid)))
    repository.close()


def _fake_video_download(self, paths, bid, position, url, year_month):
    from weibo_book.archive.media_layout import MEDIA_ROOT_NAME, MEDIA_SUBDIR_RENAMES

    year, month = year_month
    directory = paths.media_root / MEDIA_SUBDIR_RENAMES["posts"] / year / month
    directory.mkdir(parents=True, exist_ok=True)
    content = f"{bid} 视频 {position}".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    target = directory / f"{bid}_video_{position}_{digest[:16]}.mp4"
    target.write_bytes(content)
    relative = (
        f"{MEDIA_ROOT_NAME}/{MEDIA_SUBDIR_RENAMES['posts']}"
        f"/{year}/{month}/{target.name}"
    )
    return relative, digest


def _video_media_rows(repository: ArchiveRepository):
    return repository._connection.execute(
        "SELECT owner_id, role, remote_url, local_path, sha256 FROM media"
        " WHERE role = 'video' ORDER BY owner_id"
    ).fetchall()


def test_sync_backfills_missing_post_videos(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    _seed_missing_video_posts(root, ["V1"])
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    posts = {"A": _sync_post("A"), "V1": _video_post("V1")}

    monkeypatch.setattr(
        PersonalArchiveSync, "_download_post_video", _fake_video_download
    )
    PersonalArchiveSync(root, _PinnedSource(pages, posts), _IdentityProvider()).run(
        "incremental"
    )

    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("incremental")[1] == "done"
    rows = _video_media_rows(repository)
    assert [(row[0], row[1]) for row in rows] == [("V1", "video")]
    assert rows[0][2] == "https://f.video.weibocdn.com/V1.mp4"
    assert rows[0][3].startswith("媒体/微博图片/2026/09/")
    assert len(rows[0][4]) == 64
    assert repository.paths.media_file_path(rows[0][3]).is_file()
    repository.close()

    # 视频已落库后再次同步不再重复补抓
    fetch_calls = []

    class _CountingSource(_PinnedSource):
        def fetch_post(self, uid: str, bid: str) -> Post:
            fetch_calls.append(bid)
            return super().fetch_post(uid, bid)

    PersonalArchiveSync(
        root, _CountingSource(pages, posts), _IdentityProvider()
    ).run("incremental")
    assert "V1" not in fetch_calls
    repository = ArchiveRepository.open(root, "10001")
    assert len(_video_media_rows(repository)) == 1
    repository.close()


def test_video_backfill_failure_does_not_block_sync(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    _seed_missing_video_posts(root, ["V1"])
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]

    class _FailingSource(_PinnedSource):
        def fetch_post(self, uid: str, bid: str) -> Post:
            if bid == "V1":
                raise WeiboError("模拟详情抓取失败")
            return super().fetch_post(uid, bid)

    source = _FailingSource(pages, {"A": _sync_post("A")})
    monkeypatch.setattr(
        PersonalArchiveSync, "_download_post_video", _fake_video_download
    )
    result = PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")

    assert result.new_posts == 1
    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("incremental")[1] == "done"
    assert _video_media_rows(repository) == []
    repository.close()


def test_video_backfill_respects_batch_limit(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    bids = [f"V{index:03d}" for index in range(60)]
    _seed_missing_video_posts(root, bids)
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    posts = {"A": _sync_post("A")}
    posts.update({bid: _video_post(bid) for bid in bids})

    monkeypatch.setattr(
        PersonalArchiveSync, "_download_post_video", _fake_video_download
    )
    PersonalArchiveSync(root, _PinnedSource(pages, posts), _IdentityProvider()).run(
        "incremental"
    )

    repository = ArchiveRepository.open(root, "10001")
    first_batch = _video_media_rows(repository)
    assert len(first_batch) == PersonalArchiveSync._VIDEO_BACKFILL_BATCH
    # 剩余的在下一次同步继续补抓
    repository.close()
    PersonalArchiveSync(root, _PinnedSource(pages, posts), _IdentityProvider()).run(
        "incremental"
    )
    repository = ArchiveRepository.open(root, "10001")
    assert len(_video_media_rows(repository)) == 60
    repository.close()


def test_upgrade_cover_url():
    from weibo_book.archive.source import _upgrade_cover_url

    assert _upgrade_cover_url(
        "https://wx4.sinaimg.cn/crop.0.0.640.640.640/abc.jpg"
    ) == "https://wx4.sinaimg.cn/large/abc.jpg"
    # 带签名查询串的链接改写路径会失效，保持原样
    signed = "https://tvax1.sinaimg.cn/crop.0.0.1080.1080.180/abc.jpg?KID=imgbed,tva&Expires=1&ssig=x"
    assert _upgrade_cover_url(signed) == signed
    assert _upgrade_cover_url("https://wx1.sinaimg.cn/large/abc.jpg") == (
        "https://wx1.sinaimg.cn/large/abc.jpg"
    )
    assert _upgrade_cover_url("") == ""
    assert _upgrade_cover_url(None) == ""


def _seed_low_quality_video(root: Path, bid: str, url: str) -> None:
    """写入已有低清视频文件的微博，模拟早期清晰度链缺 stream_url_hd 的状态。"""
    from weibo_book.archive.sync import post_to_record

    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")
    repository.upsert_post(post_to_record(_video_post(bid)))
    repository.upsert_media(
        MediaRecord(
            "post", bid, "video", 0, url,
            f"媒体/微博图片/2026/09/{bid}_old.mp4", "ab" * 32,
        )
    )
    repository.close()


def test_low_quality_video_listing_and_confirmation(tmp_path):
    root = tmp_path / "archive"
    _seed_low_quality_video(
        root, "V1", "https://f.video.weibocdn.com/o0/aaa.mp4?label=mp4_ld&ssig=x"
    )

    repository = ArchiveRepository.open(root, "10001")
    rows = repository.list_low_quality_video_posts()
    assert [(row[0], row[2]) for row in rows] == [
        ("V1", "https://f.video.weibocdn.com/o0/aaa.mp4?label=mp4_ld&ssig=x")
    ]
    repository.confirm_low_quality_video("V1")
    repository.confirm_low_quality_video("V1")  # 幂等
    assert repository.list_low_quality_video_posts() == []
    repository.close()

    repository = ArchiveRepository.open(root, "10001")
    assert repository.list_low_quality_video_posts() == []
    repository.close()


def test_low_quality_video_recrawl_confirms_when_no_hd(tmp_path, monkeypatch):
    from weibo_book.archive import video_quality as quality
    root = tmp_path / "archive"
    old = "https://v.test/old.mp4?label=mp4_720p&template=1280x720"
    _seed_low_quality_video(root, "V1", old)
    class Source:
        def fetch_video_playback_url(self, bid): return old
    repo = ArchiveRepository.open(root, "10001")
    result = quality.scan(repo, Source())
    assert result == {"unchecked_count": 0, "available_count": 0, "failed_count": 0}
    assert _video_media_rows(repo)[0][2] == old
    repo.close()


def test_low_quality_video_recrawl_upgrades_to_hd(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync

    root = tmp_path / "archive"
    old_url = "https://f.video.weibocdn.com/o0/aaa.mp4?label=mp4_ld&Expires=1&ssig=x"
    new_url = "https://f.video.weibocdn.com/o0/aaa.mp4?label=mp4_1080p&template=1920x1080&Expires=2&ssig=y"
    _seed_low_quality_video(root, "V1", old_url)
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    post = _video_post("V1")
    post.media[0].url = new_url

    from weibo_book.archive import video_quality as quality
    class Source(_PinnedSource):
        def fetch_video_playback_url(self, bid): return new_url
    source = Source(pages, {"A": _sync_post("A"), "V1": post})
    repo = ArchiveRepository.open(root, "10001")
    quality.scan(repo, source)
    repo.close()
    monkeypatch.setattr(PersonalArchiveSync, "_download_post_video", _fake_video_download)
    PersonalArchiveSync(
        root, source, _IdentityProvider(), video_upgrade_limit=10,
    ).run("incremental")

    repository = ArchiveRepository.open(root, "10001")
    rows = _video_media_rows(repository)
    assert len(rows) == 1
    assert rows[0][2] == new_url  # remote_url 已覆盖为高清
    assert rows[0][3].startswith("媒体/微博图片/2026/09/")
    assert repository.paths.media_file_path(rows[0][3]).is_file()
    assert repository.list_low_quality_video_posts() == []
    repository.close()


@pytest.mark.parametrize("kind", ["avatar", "cover"])
def test_profile_asset_identity_ignores_size_and_signature(tmp_path, kind):
    repo = _create_archive_with_history(tmp_path)
    first = "2026-09-06T00:00:00+00:00"
    old = "https://wx1.sinaimg.cn/crop.0.0.640.640.640/same.jpg?Expires=1&ssig=a"
    new = "https://wx2.sinaimg.cn/large/same.jpg?Expires=2&ssig=b"
    assert repo.record_profile_asset(kind, old, first_seen_at=first, run_id="one")
    assert not repo.record_profile_asset(kind, new, local_path="media/covers/same.jpg",
                                       first_seen_at=_SEED_TIME, run_id="two")
    rows = repo.list_profile_assets(kind)
    assert len(rows) == 1
    assert rows[0].first_seen_at == _SEED_TIME
    assert rows[0].local_path == "media/covers/same.jpg"
    repo.close()


def test_old_duplicate_rows_keep_earliest_time_and_explicit_current(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot
    repo = _create_archive_with_history(tmp_path)
    for url, local, when in [
        ("https://wx1.sinaimg.cn/crop.1/blue.jpg?Expires=1", "", "2026-09-06"),
        ("https://wx1.sinaimg.cn/large/blue.jpg", "media/covers/blue.jpg", "2026-09-07"),
        ("https://wx1.sinaimg.cn/large/history.jpg", "media/covers/history.jpg", "2026-09-08"),
    ]:
        repo._connection.execute(
            "INSERT INTO profile_assets(kind, remote_url, local_path, sha256, first_seen_at, run_id) VALUES ('cover', ?, ?, '', ?, 'old')",
            (url, local, when),
        )
    repo.set_current_profile_asset("cover", "https://wx2.sinaimg.cn/crop.2/blue.jpg?Expires=3")
    snapshot = ArchiveRenderSnapshot.from_repository(repo)
    assert len(snapshot.cover_history) == 2
    current = [x for x in snapshot.cover_history if x["current"]]
    assert len(current) == 1
    assert current[0]["first_seen_at"] == "2026-09-06"
    assert current[0]["url"].endswith("/blue.jpg")
    assert snapshot.user["cover_url"] == current[0]["url"]
    assert len(repo.list_profile_assets("cover")) == 3
    repo.set_current_profile_asset("cover", "")
    assert not any(x["current"] for x in ArchiveRenderSnapshot.from_repository(repo).cover_history)
    repo.close()


def test_sync_retries_same_image_with_new_signature(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync
    repo = _create_archive_with_history(tmp_path)
    repo.record_profile_asset("avatar", "https://wx1.sinaimg.cn/crop.1/a.jpg?Expires=1",
                              first_seen_at=_SEED_TIME, run_id="one")
    calls = []
    def download(self, paths, kind, url):
        calls.append(url)
        return "media/avatars/a.jpg", "ab" * 32
    monkeypatch.setattr(PersonalArchiveSync, "_download_profile_asset", download)
    sync = PersonalArchiveSync(repo._root, _PinnedSource([], {}), _IdentityProvider())
    url = "https://wx1.sinaimg.cn/large/a.jpg?Expires=2"
    sync._record_one_profile_asset(repo, "two", "avatar", url, _SEED_TIME)
    assert calls == [url]
    assert len(repo.list_profile_assets("avatar")) == 1
    assert repo.list_profile_assets("avatar")[0].local_path == "media/avatars/a.jpg"
    repo.close()


def test_current_profile_asset_survives_reopen_and_kind_isolation(tmp_path):
    repo = _create_archive_with_history(tmp_path)
    root = repo._root
    repo._connection.commit()
    with repo.transaction():
        repo.set_current_profile_asset("avatar", "https://wx1.sinaimg.cn/large/same.jpg")
        repo.set_current_profile_asset("cover", "https://wx1.sinaimg.cn/large/other.jpg")
    repo.close()
    repo = ArchiveRepository.open(root, "10001")
    assert repo.get_current_profile_asset("avatar").endswith("/same.jpg")
    assert repo.get_current_profile_asset("cover").endswith("/other.jpg")
    repo.close()


def test_unknown_current_does_not_mark_latest_historical_picture(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot
    repo = _create_archive_with_history(tmp_path)
    repo.record_profile_asset("cover", "https://wx1.sinaimg.cn/large/history.jpg",
                              local_path="media/covers/history.jpg",
                              first_seen_at=_SEED_TIME, run_id="old")
    snapshot = ArchiveRenderSnapshot.from_repository(repo)
    assert len(snapshot.cover_history) == 1
    assert snapshot.cover_history[0]["current"] is False
    assert snapshot.user["cover_url"] == ""
    repo.close()


def test_video_recheck_includes_720p_and_ignores_old_confirmation(tmp_path):
    repo = _create_archive_with_history(tmp_path)
    repo.upsert_post(_post_record('V720'))
    url = 'https://f.video.weibocdn.com/v.mp4?label=mp4_720p&template=1280x720'
    repo.upsert_media(MediaRecord('post','V720','video',0,url,'media/v.mp4',''))
    repo._write_meta('video_ld_confirmed',['V720'])
    assert [x[0] for x in repo.list_low_quality_video_posts()] == ['V720']
    repo.close()


def test_video_pc_failure_remains_retryable(tmp_path, monkeypatch):
    from weibo_book.archive.sync import PersonalArchiveSync
    root=tmp_path/'archive'
    old='https://f.video.weibocdn.com/v.mp4?label=mp4_hd&template=852x480'
    _seed_low_quality_video(root,'V1',old)
    class Source(_PinnedSource):
        def fetch_video_playback_url(self,bid):
            raise WeiboError('模拟高清接口暂时失败')
    repo=ArchiveRepository.open(root,'10001')
    sync=PersonalArchiveSync(root,Source([],{ 'V1': _video_post('V1')}),_IdentityProvider())
    from weibo_book.archive import video_quality as quality
    quality._remember(repo, repo.list_media_for_render()[0], "available")
    quality.apply_selected(repo, sync.source, 'one', 10, sync._download_post_video)
    assert [x[0] for x in repo.list_low_quality_video_posts()] == ['V1']
    repo.close()


@pytest.mark.parametrize('result', ['better', 'same', 'empty', 'download_error'])
def test_stored_video_pc_verification_and_retry_state(tmp_path, monkeypatch, result):
    from weibo_book.archive.sync import PersonalArchiveSync
    root=tmp_path/'archive'
    old='https://f.video.weibocdn.com/old.mp4?label=mp4_720p&template=1280x720'
    new='https://f.video.weibocdn.com/new.mp4?label=mp4_1080p&template=1920x1080'
    _seed_low_quality_video(root,'V1',old)
    class Source(_PinnedSource):
        def fetch_video_playback_url(self,bid):
            return None if result=='empty' else old if result=='same' else new
        def fetch_post(self,*args):
            raise AssertionError('已有视频无需通过移动接口重复抓取正文')
    def download(self,paths,bid,position,url,year_month):
        if result=='download_error':
            raise WeiboError('模拟下载失败')
        assert url==new
        return 'media/new.mp4','ab'*32
    monkeypatch.setattr(PersonalArchiveSync,'_download_post_video',download)
    repo=ArchiveRepository.open(root,'10001')
    from weibo_book.archive import video_quality as quality
    quality._remember(repo, repo.list_media_for_render()[0], "available")
    sync=PersonalArchiveSync(root,Source([],{}),_IdentityProvider())
    quality.apply_selected(repo,sync.source,'one',10,sync._download_post_video)
    row=_video_media_rows(repo)[0]
    assert row[2] == (new if result=='better' else old)
    state=quality.summary(repo)
    assert bool(state['available_count']+state['failed_count']) == (result in {'empty','download_error'})
    repo.close()


def test_pc_single_video_upgrade_does_not_replace_multi_video_post(tmp_path):
    from dataclasses import replace
    from weibo_book.archive.sync import PersonalArchiveSync
    root=tmp_path/'archive'
    old='https://v.test/low.mp4?label=mp4_720p&template=1280x720'
    _seed_low_quality_video(root,'V1',old)
    repo=ArchiveRepository.open(root,'10001')
    repo.upsert_post(replace(repo.get_post('V1'),media_signature=[{'type':'video'},{'type':'video'}]))
    calls=[]
    class Source(_PinnedSource):
        def fetch_video_playback_url(self,bid):
            calls.append(bid)
            return old
    from weibo_book.archive import video_quality as quality
    quality.scan(repo, Source([],{}))
    assert calls==[]
    assert _video_media_rows(repo)[0][2]==old
    repo.close()


@pytest.mark.parametrize('uid', ['10001', '20002'])
@pytest.mark.parametrize('fail_first', [False, True])
def test_incremental_backup_executes_chosen_upgrades_for_each_archive(tmp_path, monkeypatch, uid, fail_first):
    from dataclasses import replace
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record
    root=tmp_path/uid
    old='https://v.test/old.mp4?label=mp4_720p&template=1280x720'
    high='https://v.test/high.mp4?label=mp4_1080p&template=1920x1080'
    repo=ArchiveRepository.create(root,uid=uid,screen_name='测试博主')
    repo.upsert_post(post_to_record(replace(_video_post('V1'),uid=uid)))
    repo.upsert_media(MediaRecord('post','V1','video',0,old,'media/old.mp4','ab'*32))
    repo.close()
    class Identity:
        def whoami(self):return {'uid':uid,'screen_name':'测试博主'}
    class Source(_PinnedSource):
        fail=fail_first
        checks=0
        def fetch_video_playback_url(self,bid):
            assert bid=='V1'
            self.checks+=1
            if self.fail:raise WeiboError('模拟暂时网络失败')
            return high
    source=Source([ProfilePage([ProfileItem('A')],is_last=True)],{'A':replace(_sync_post('A'),uid=uid)})
    monkeypatch.setattr(PersonalArchiveSync,'_download_post_video',_fake_video_download)
    from weibo_book.archive import video_quality as quality
    repo=ArchiveRepository.open(root,uid)
    quality.scan(repo,source)
    repo.close()
    PersonalArchiveSync(root,source,Identity(),video_upgrade_limit=10).run('incremental')
    if fail_first:
        repo=ArchiveRepository.open(root,uid)
        assert _video_media_rows(repo)[0][2]==old
        assert len(repo.list_low_quality_video_posts())==1
        repo.close()
        source.fail=False
        repo=ArchiveRepository.open(root,uid)
        quality.scan(repo,source)
        repo.close()
        PersonalArchiveSync(root,source,Identity(),video_upgrade_limit=10).run('incremental')
    repo=ArchiveRepository.open(root,uid)
    assert _video_media_rows(repo)[0][2]==high
    assert repo.get_latest_sync_status('incremental')[1]=='done'
    assert repo.list_low_quality_video_posts()==[]
    repo.close()
    checks=source.checks
    PersonalArchiveSync(root,source,Identity()).run('incremental')
    assert source.checks==checks


def test_normal_incremental_does_not_silently_upgrade_legacy_videos(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync
    root=tmp_path/'archive'
    old='https://v.test/old.mp4?label=mp4_720p&template=1280x720'
    _seed_low_quality_video(root,'V1',old)
    class Source(_PinnedSource):
        def fetch_video_playback_url(self,bid):raise AssertionError('没有选择，不应检查或下载旧视频')
    source=Source([ProfilePage([ProfileItem('A')],is_last=True)],{'A':_sync_post('A')})
    PersonalArchiveSync(root,source,_IdentityProvider()).run('incremental')
    repo=ArchiveRepository.open(root,'10001')
    assert _video_media_rows(repo)[0][2]==old
    repo.close()
