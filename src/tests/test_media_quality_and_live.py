"""视频画质升级、封面列表与直播回放补抓的测试（2026-09-08 第二轮修复）。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weibo_book.archive.discovery import ProfileItem, ProfilePage
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import MediaRecord, PostRecord
from weibo_book.errors import OperationPaused, WeiboError
from weibo_book.models import Comment, MediaType, Post, PostMedia


# ===== video_playback：画质打分与播放列表解析 =====


def test_video_quality_score_orders_by_label_and_template():
    from weibo_book.archive.video_playback import video_quality_score

    ld = video_quality_score("https://f.video.weibocdn.com/x.mp4?label=mp4_ld&template=640x360")
    hd = video_quality_score("https://f.video.weibocdn.com/x.mp4?label=mp4_hd&template=852x480")
    p720 = video_quality_score("https://f.video.weibocdn.com/x.mp4?label=mp4_720p&template=1280x720")
    p1080 = video_quality_score("https://f.video.weibocdn.com/x.mp4?label=mp4_1080p&template=1920x1080")
    assert ld < hd < p720 < p1080
    assert video_quality_score("") == (0, 0)
    assert video_quality_score(None) == (0, 0)
    assert video_quality_score("https://example.test/plain.mp4") == (0, 0)


def test_parse_best_playback_url_picks_highest_video_quality():
    from weibo_book.archive.video_playback import parse_best_playback_url

    payload = {
        "page_info": {
            "media_info": {
                "playback_list": [
                    {"play_info": {"mime": "video/mp4", "url": "https://v.test/ld.mp4?label=mp4_ld&template=640x360"}},
                    {"play_info": {"mime": "video/mp4", "url": "https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080"}},
                    {"play_info": {"mime": "video/mp4", "url": "https://v.test/720.mp4?label=mp4_720p&template=1280x720"}},
                    {"play_info": {"mime": "application/x-mpegURL", "url": "https://v.test/master.m3u8"}},
                ]
            }
        }
    }
    assert parse_best_playback_url(payload) == (
        "https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080"
    )
    assert parse_best_playback_url({}) is None
    assert parse_best_playback_url({"page_info": {"media_info": {}}}) is None
    assert parse_best_playback_url("不是字典") is None


# ===== 封面列表解析 =====


def test_parse_cover_urls_splits_upgrades_and_dedupes():
    from weibo_book.archive.source import _parse_cover_urls

    payload = {
        "data": {
            "user": {
                "cover_image_phone": (
                    "https://wx1.sinaimg.cn/crop.0.0.640.640.640/aaa.jpg;"
                    " https://wx2.sinaimg.cn/crop.0.0.640.640.640/bbb.jpg ;"
                    "https://wx1.sinaimg.cn/crop.0.0.640.640.640/aaa.jpg;"
                    "not-a-url;"
                    "https://wx3.sinaimg.cn/crop.0.0.100.100.100/ccc.jpg?KID=1&Expires=2&ssig=3"
                )
            }
        }
    }
    assert _parse_cover_urls(payload) == [
        "https://wx1.sinaimg.cn/large/aaa.jpg",
        "https://wx2.sinaimg.cn/large/bbb.jpg",
        # 带签名查询串的链接不改写，避免失效
        "https://wx3.sinaimg.cn/crop.0.0.100.100.100/ccc.jpg?KID=1&Expires=2&ssig=3",
    ]
    assert _parse_cover_urls({}) == []
    assert _parse_cover_urls({"data": {"user": {"cover_image_phone": ""}}}) == []
    assert _parse_cover_urls("不是字典") == []


# ===== source 层：fake session =====


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

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append((url, dict(params or {}), dict(headers or {})))
        handler = self._routes[url]
        if isinstance(handler, Exception):
            raise handler
        return handler


class _FakeExtractor:
    def __init__(self, session):
        self.client = type("_FakeClient", (), {"session": session})()


def _make_source(session):
    from weibo_book.archive.source import WeiboArchiveSource

    return WeiboArchiveSource(
        _FakeExtractor(session), self_uid="10001", image_quality=None
    )


def test_fetch_cover_list_reads_profile_info():
    from weibo_book.archive.source import PROFILE_INFO_URL

    session = _FakeSession({
        PROFILE_INFO_URL: _FakeResponse({
            "data": {"user": {"cover_image_phone": "https://wx1.sinaimg.cn/crop.0.0.640.640.640/a.jpg"}}
        })
    })
    source = _make_source(session)
    assert source.fetch_cover_list("10001") == ["https://wx1.sinaimg.cn/large/a.jpg"]
    url, params, headers = session.requests[0]
    assert url == PROFILE_INFO_URL
    assert headers.get("Referer") == "https://weibo.com/"


def test_fetch_cover_list_maps_pause_and_error():
    from weibo_book.archive.source import PROFILE_INFO_URL

    source = _make_source(_FakeSession({PROFILE_INFO_URL: _FakeResponse({}, 429)}))
    with pytest.raises(OperationPaused):
        source.fetch_cover_list("10001")
    source = _make_source(_FakeSession({PROFILE_INFO_URL: _FakeResponse({}, 403)}))
    with pytest.raises(OperationPaused):
        source.fetch_cover_list("10001")
    source = _make_source(_FakeSession({PROFILE_INFO_URL: _FakeResponse({}, 500)}))
    with pytest.raises(WeiboError, match="读取封面列表失败"):
        source.fetch_cover_list("10001")


def test_fetch_video_playback_url_and_upgrade():
    from weibo_book.archive.source import WeiboArchiveSource

    payload = {
        "page_info": {
            "media_info": {
                "playback_list": [
                    {"play_info": {"mime": "video/mp4", "url": "https://v.test/480.mp4?label=mp4_hd&template=852x480"}},
                    {"play_info": {"mime": "video/mp4", "url": "https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080"}},
                ]
            }
        }
    }
    session = _FakeSession({WeiboArchiveSource.STATUS_SHOW_AJAX_URL: _FakeResponse(payload)})
    source = _make_source(session)

    assert source.fetch_video_playback_url("B1") == (
        "https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080"
    )
    assert session.requests[0][1] == {"id": "B1"}

    post = Post(
        bid="B1", uid="10001", user_name="测试", user_avatar="", text="视频",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        media=[PostMedia(type=MediaType.VIDEO, url="https://v.test/480.mp4?label=mp4_hd&template=852x480")],
    )
    upgraded = source._upgrade_video_playback(post)
    assert upgraded.media[0].url == "https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080"


def test_upgrade_video_playback_keeps_original_when_not_better_or_multi():
    session = _FakeSession({})
    source = _make_source(session)
    source.fetch_video_playback_url = lambda bid: "https://v.test/360.mp4?label=mp4_ld&template=640x360"

    post = Post(
        bid="B1", uid="10001", user_name="测试", user_avatar="", text="视频",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        media=[PostMedia(type=MediaType.VIDEO, url="https://v.test/1080.mp4?label=mp4_1080p&template=1920x1080")],
    )
    assert source._upgrade_video_playback(post).media[0].url.startswith("https://v.test/1080.mp4")

    mixed = Post(
        bid="B2", uid="10001", user_name="测试", user_avatar="", text="混合",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        media=[
            PostMedia(type=MediaType.VIDEO, url="https://v.test/a.mp4?label=mp4_ld&template=640x360"),
            PostMedia(type=MediaType.IMAGE, url="https://p.test/a.jpg"),
        ],
    )
    assert source._upgrade_video_playback(mixed).media[0].url.startswith("https://v.test/a.mp4")


def test_upgrade_video_playback_failure_keeps_original():
    session = _FakeSession({})
    source = _make_source(session)

    def failing(bid):
        raise WeiboError("模拟读取失败")

    source.fetch_video_playback_url = failing
    post = Post(
        bid="B1", uid="10001", user_name="测试", user_avatar="", text="视频",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        media=[PostMedia(type=MediaType.VIDEO, url="https://v.test/480.mp4?label=mp4_hd&template=852x480")],
    )
    assert source._upgrade_video_playback(post).media[0].url.startswith("https://v.test/480.mp4")

    def paused(bid):
        raise OperationPaused("限流", pause_reason="rate_limited")

    source.fetch_video_playback_url = paused
    with pytest.raises(OperationPaused):
        source._upgrade_video_playback(post)


# ===== live_replay：解析 =====


def test_parse_live_id():
    from weibo_book.archive.live_replay import parse_live_id

    assert parse_live_id("https://weibo.com/l/wblive/p/show/1022:2321325340611543957577") == (
        "1022:2321325340611543957577"
    )
    assert parse_live_id("https://m.yizhibo.com/l/ZgjrWCrpFowjC2D7.html") is None
    assert parse_live_id("") is None
    assert parse_live_id(None) is None


def test_parse_replay_payload():
    from weibo_book.archive.live_replay import parse_replay_payload

    payload = {"data": {"replay_origin_url": "https://live.test/x.m3u8", "cover": "c", "title": "t"}}
    assert parse_replay_payload(payload) == {
        "replay_url": "https://live.test/x.m3u8", "cover": "c", "title": "t",
    }
    assert parse_replay_payload({"data": {}}) is None
    assert parse_replay_payload({"data": {"replay_origin_url": "not-http"}}) is None
    assert parse_replay_payload(None) is None


def test_m3u8_segments_and_rewrite():
    from weibo_book.archive.live_replay import (
        parse_m3u8_segments,
        resolve_segment_url,
        rewrite_m3u8,
    )

    text = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:12.0,\nseg0.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:10.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
    )
    assert parse_m3u8_segments(text) == ["seg0.ts", "seg1.ts"]
    rewritten = rewrite_m3u8(text, ["local0.ts", "local1.ts"])
    assert "local0.ts" in rewritten and "local1.ts" in rewritten
    assert "seg0.ts" not in rewritten
    assert rewritten.count("#EXT-X-DISCONTINUITY") == 1
    assert resolve_segment_url("https://x.test/a/b.m3u8", "seg0.ts") == "https://x.test/a/seg0.ts"


def test_fetch_live_replay_reads_show_pc_live():
    from weibo_book.archive.source import WeiboArchiveSource

    session = _FakeSession({
        WeiboArchiveSource.LIVE_SHOW_URL: _FakeResponse(
            {"data": {"replay_origin_url": "https://live.test/x.m3u8", "title": "回放"}}
        )
    })
    source = _make_source(session)
    result = source.fetch_live_replay("https://weibo.com/l/wblive/p/show/1022:123")
    assert result == {"replay_url": "https://live.test/x.m3u8", "title": "回放"}
    assert session.requests[0][1] == {"live_id": "1022:123"}
    # 非微博直播页不发请求
    assert source.fetch_live_replay("https://m.yizhibo.com/l/abc.html") is None
    assert len(session.requests) == 1


def test_fetch_live_replay_pause_and_error():
    from weibo_book.archive.source import WeiboArchiveSource

    page = "https://weibo.com/l/wblive/p/show/1022:123"
    source = _make_source(_FakeSession({WeiboArchiveSource.LIVE_SHOW_URL: _FakeResponse({}, 429)}))
    with pytest.raises(OperationPaused):
        source.fetch_live_replay(page)
    source = _make_source(_FakeSession({WeiboArchiveSource.LIVE_SHOW_URL: _FakeResponse({}, 500)}))
    with pytest.raises(WeiboError, match="读取直播回放失败"):
        source.fetch_live_replay(page)


# ===== repository：直播帖查询与确认名单 =====


def _live_post_record(bid: str, url: str = "https://weibo.com/l/wblive/p/show/1022:123"):
    return PostRecord(
        bid=bid,
        uid="10001",
        text="直播",
        created_at="2026-09-07T12:00:00+00:00",
    )


def _set_live_link_card(repository, bid: str, url: str) -> None:
    repository._connection.execute(
        "UPDATE posts SET link_card_json = ? WHERE bid = ?",
        (json.dumps({"type": "live", "url": url, "title": "直播"}, ensure_ascii=False), bid),
    )


def test_list_live_posts_without_replay_and_confirm(tmp_path):
    repository = ArchiveRepository.create(tmp_path / "archive", uid="10001", screen_name="测试")
    repository.upsert_post(_live_post_record("L1"))
    _set_live_link_card(repository, "L1", "https://weibo.com/l/wblive/p/show/1022:123")
    repository.upsert_post(PostRecord(bid="P1", uid="10001", text="普通"))

    assert [bid for bid, _, _ in repository.list_live_posts_without_replay()] == ["L1"]

    repository.confirm_live_replay_unavailable("L1")
    assert repository.list_live_posts_without_replay() == []

    repository2 = ArchiveRepository.create(tmp_path / "archive2", uid="10001", screen_name="测试")
    repository2.upsert_post(_live_post_record("L2"))
    _set_live_link_card(repository2, "L2", "https://weibo.com/l/wblive/p/show/1022:456")
    repository2.upsert_media(
        MediaRecord(
            "post", "L2", "live_replay", 900,
            "https://live.test/x.m3u8", "媒体/微博图片/2026/09/L2_live_x.m3u8", "ab" * 32,
        )
    )
    assert repository2.list_live_posts_without_replay() == []
    repository.close()
    repository2.close()


# ===== sync：直播回放补抓 =====


class _IdentityProvider:
    def whoami(self) -> dict:
        return {"uid": "10001", "screen_name": "测试用户"}


class _LiveSource:
    def __init__(self, pages, posts, replay):
        self._pages = pages
        self._posts = posts
        self._replay = replay
        self.replay_requests = []

    def iter_profile_pages(self, uid: str):
        yield from self._pages

    def fetch_post(self, uid: str, bid: str) -> Post:
        return self._posts[bid]

    def fetch_recent_comments(self, post_id: str, limit: int = 10) -> list[Comment]:
        return []

    def fetch_live_replay(self, page_url: str):
        self.replay_requests.append(page_url)
        if isinstance(self._replay, Exception):
            raise self._replay
        return self._replay


def _sync_post(bid: str) -> Post:
    return Post(
        bid=bid, uid="10001", user_name="测试用户", user_avatar="", text="正文",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


_PLAYLIST = "#EXTM3U\n#EXTINF:1.0,\nseg0.ts\n#EXTINF:1.0,\nseg1.ts\n#EXT-X-ENDLIST\n"


def _fake_download_file(client, url, dest, **kwargs):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.endswith(".m3u8"):
        dest.write_text(_PLAYLIST, encoding="utf-8")
    else:
        dest.write_bytes(f"分段 {url}".encode("utf-8"))
    return True


def test_sync_backfills_live_replay(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")
    repository.upsert_post(post_to_record(_sync_post("OLD")))
    repository._connection.execute(
        "UPDATE posts SET link_card_json = ? WHERE bid = 'OLD'",
        (json.dumps(
            {"type": "live", "url": "https://weibo.com/l/wblive/p/show/1022:123"},
            ensure_ascii=False,
        ),),
    )
    repository.close()

    replay = {"replay_url": "https://live.test/x/index.m3u8", "title": "回放"}
    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    source = _LiveSource(pages, {"A": _sync_post("A")}, replay)
    monkeypatch.setattr(sync_module, "download_file", _fake_download_file)

    PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")

    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("incremental")[1] == "done"
    rows = repository._connection.execute(
        "SELECT role, position, remote_url, local_path FROM media"
        " WHERE owner_id = 'OLD' ORDER BY position"
    ).fetchall()
    assert [row[0] for row in rows] == ["live_replay", "live_replay_segment", "live_replay_segment"]
    assert [row[1] for row in rows] == [900, 1000, 1001]
    playlist_path = rows[0][3]
    assert playlist_path.endswith(".m3u8")
    assert playlist_path.startswith("媒体/微博图片/2026/09/OLD_live_")
    text = repository.paths.media_file_path(playlist_path).read_text(encoding="utf-8")
    assert "seg0.ts" not in text
    seg_names = [row[3].rsplit("/", 1)[-1] for row in rows[1:]]
    assert all(name in text for name in seg_names)
    for row in rows:
        assert repository.paths.media_file_path(row[3]).is_file()
    repository.close()

    # 已落库后再次同步不再重复请求回放接口
    PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")
    assert len(source.replay_requests) == 1


def test_sync_marks_unavailable_live_replay(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")
    repository.upsert_post(post_to_record(_sync_post("OLD")))
    repository._connection.execute(
        "UPDATE posts SET link_card_json = ? WHERE bid = 'OLD'",
        (json.dumps(
            {"type": "live", "url": "https://m.yizhibo.com/l/abc.html"},
            ensure_ascii=False,
        ),),
    )
    repository.close()

    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    source = _LiveSource(pages, {"A": _sync_post("A")}, None)
    monkeypatch.setattr(sync_module, "download_file", _fake_download_file)

    PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")

    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("incremental")[1] == "done"
    # 一直播外链不属于微博直播页：不发请求，直接记入无回放名单
    assert source.replay_requests == []
    assert repository.list_live_posts_without_replay() == []
    repository.close()


def test_sync_live_replay_failure_does_not_block(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, uid="10001", screen_name="测试用户")
    repository.upsert_post(post_to_record(_sync_post("OLD")))
    repository._connection.execute(
        "UPDATE posts SET link_card_json = ? WHERE bid = 'OLD'",
        (json.dumps(
            {"type": "live", "url": "https://weibo.com/l/wblive/p/show/1022:123"},
            ensure_ascii=False,
        ),),
    )
    repository.close()

    pages = [ProfilePage([ProfileItem("A")], is_last=True)]
    source = _LiveSource(pages, {"A": _sync_post("A")}, WeiboError("模拟回放接口失败"))
    monkeypatch.setattr(sync_module, "download_file", _fake_download_file)

    result = PersonalArchiveSync(root, source, _IdentityProvider()).run("incremental")

    assert result.new_posts == 1
    repository = ArchiveRepository.open(root, "10001")
    assert repository.get_latest_sync_status("incremental")[1] == "done"
    # 失败不留确认名单，下次同步还会重试
    assert [bid for bid, _, _ in repository.list_live_posts_without_replay()] == ["OLD"]
    repository.close()


# ===== 渲染：hls 投影、hls.js 引入、置顶去重、话题过滤 =====


def test_post_media_projects_live_replay_as_hls_video():
    from weibo_book.archive.render_snapshot import _post_media

    records = [
        MediaRecord(
            "post", "B", "live_replay", 900,
            "https://live.test/x.m3u8", "媒体/微博图片/2026/09/B_live_x.m3u8", "ab" * 32,
        ),
        MediaRecord(
            "post", "B", "live_replay_segment", 1000,
            "https://live.test/seg0.ts", "媒体/微博图片/2026/09/B_live_x_seg0000.ts", "",
        ),
    ]
    items = _post_media(records, "prefix/")
    assert len(items) == 1
    assert items[0]["kind"] == "video"
    assert items[0]["hls"] is True
    assert items[0]["local_path"] == "媒体/微博图片/2026/09/B_live_x.m3u8"


def test_render_includes_hls_library_only_when_needed(tmp_path):
    from weibo_book.archive.render_snapshot import (
        HLS_LIBRARY_NAME,
        ArchiveRenderer,
        _snapshot_needs_hls,
        ArchiveRenderSnapshot,
    )

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "固定名字")
    repository.upsert_post(
        PostRecord(bid="A", uid="10001", text="正文", created_at="2026-09-07T12:00:00+00:00")
    )
    snapshot = ArchiveRenderSnapshot.from_repository(repository)
    assert not _snapshot_needs_hls(snapshot)
    renderer = ArchiveRenderer(repository)
    renderer.render_all(root, render_pdf=lambda html, path: path.write_bytes(b"pdf"))
    html = (root / "微博书.html").read_text(encoding="utf-8")
    assert HLS_LIBRARY_NAME not in html
    assert not (root / "数据与媒体" / "数据" / HLS_LIBRARY_NAME).exists()

    media_dir = root / "数据与媒体" / "媒体" / "微博图片" / "2026" / "09"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "A_live_x.m3u8").write_text(_PLAYLIST, encoding="utf-8")
    repository.upsert_media(
        MediaRecord(
            "post", "A", "live_replay", 900,
            "https://live.test/x.m3u8", "媒体/微博图片/2026/09/A_live_x.m3u8", "ab" * 32,
        )
    )
    snapshot = ArchiveRenderSnapshot.from_repository(repository)
    assert _snapshot_needs_hls(snapshot)
    renderer.render_all(root, render_pdf=lambda html, path: path.write_bytes(b"pdf"))
    html = (root / "微博书.html").read_text(encoding="utf-8")
    assert '<script src="数据与媒体/数据/hls.min.js"></script>' in html
    installed = root / "数据与媒体" / "数据" / HLS_LIBRARY_NAME
    assert installed.is_file()
    assert installed.stat().st_size > 100_000
    repository.close()


def test_pin_history_dedupes_identical_consecutive_batches(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repository = ArchiveRepository.create(tmp_path / "archive", "10001", "固定名字")
    repository.upsert_post(
        PostRecord(bid="A", uid="10001", text="置顶", created_at="2026-09-01T00:00:00+00:00")
    )
    repository.record_pin_observations("run-1", "2026-09-01T00:00:00+00:00", [("A", 1)])
    repository.record_pin_observations("run-2", "2026-09-02T00:00:00+00:00", [("A", 1)])
    repository.record_pin_observations("run-3", "2026-09-03T00:00:00+00:00", [])

    snapshot = ArchiveRenderSnapshot.from_repository(repository)
    assert [list(batch["bids"]) for batch in snapshot.pin_history] == [["A"]]
    repository.close()


def test_topic_link_card_filtered_at_converter_and_snapshot():
    from weibo_book.archive.render_snapshot import _project_link
    from weibo_book.post_converter import extract_link_card

    assert _project_link({"type": "topic", "title": "#话题#"}) is None
    assert _project_link({"type": "search_topic", "title": "#话题#", "description": "0讨论 0阅读"}) is None
    assert _project_link({"type": "webpage", "title": "链接", "url": "https://x.test"}) is not None
    crawl_post = type("_CrawlPost", (), {"raw_data": {"page_info": {"type": "topic", "page_title": "#话题#"}}})()
    assert extract_link_card(crawl_post) is None
    crawl_post2 = type("_CrawlPost", (), {"raw_data": {"page_info": {"type": "search_topic", "page_title": "#话题#"}}})()
    assert extract_link_card(crawl_post2) is None


def test_template_post_text_marks_topics():
    template = Path(__file__).resolve().parents[1] / "weibo_book" / "templates" / "book_interactive.html"
    source = template.read_text(encoding="utf-8")
    assert "function postText(" in source
    assert "span" in source and "topic" in source
    assert "setupHlsPlayback" in source
    assert "hls_script_src" in source


@pytest.mark.parametrize("count, expected", [(13537480, 13537480), (0, 0), ("1353.7万", None), (True, None), (-1, None), (None, None)])
def test_profile_assets_use_verified_pc_fields(count, expected):
    from weibo_book.archive.source import PROFILE_INFO_URL
    session = _FakeSession({PROFILE_INFO_URL: _FakeResponse({"data": {"user": {
        "avatar_hd": "https://wx1.sinaimg.cn/crop.1/avatar.jpg?Expires=1",
        "cover_image_phone": "https://wx1.sinaimg.cn/crop.1/blue.jpg;https://wx1.sinaimg.cn/large/history.jpg",
        "followers_count": count,
    }}})})
    assert _make_source(session).fetch_profile_assets("10001") == (
        "https://wx1.sinaimg.cn/crop.1/avatar.jpg?Expires=1",
        "https://wx1.sinaimg.cn/large/blue.jpg", expected,
    )


def test_live_backfill_is_media_phase_and_pause_is_not_swallowed(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record
    root = tmp_path / 'archive'
    repo = ArchiveRepository.create(root, '10001', '测试用户')
    post = _sync_post('OLD')
    repo.upsert_post(post_to_record(post))
    repo._connection.execute("UPDATE posts SET link_card_json=? WHERE bid='OLD'", (
        json.dumps({'type':'live', 'url':'https://weibo.com/l/wblive/p/show/1022:123'}),))
    repo.close()
    events = []
    source = _LiveSource([ProfilePage([], is_last=True)], {}, {'replay_url':'https://live.test/list.m3u8'})
    def paused(client, url, dest, **kwargs):
        if url.endswith('.m3u8'):
            return _fake_download_file(client, url, dest, **kwargs)
        assert events[-1]['phase'] == 'media'
        assert events[-1]['subphase'] == 'live_replay'
        raise OperationPaused('任务已暂停')
    monkeypatch.setattr(sync_module, 'download_file', paused)
    with pytest.raises(OperationPaused):
        PersonalArchiveSync(root, source, _IdentityProvider(), progress_callback=events.append).run('incremental')
    repo = ArchiveRepository.open(root, '10001')
    assert repo.get_latest_sync_status('incremental')[1] == 'paused'
    assert repo.list_live_posts_without_replay()
    repo.close()
    monkeypatch.setattr(sync_module, 'download_file', _fake_download_file)
    PersonalArchiveSync(root, source, _IdentityProvider(), progress_callback=events.append).run('incremental')
    assert next(e for e in events if e.get('unit') == 'segment' and e['total'] is not None)['total'] == 2
    last_media = max(i for i,e in enumerate(events) if e['phase'] == 'media')
    assert any(e['phase'] == 'generate' for e in events[last_media+1:])


def test_replay_publication_pause_reuses_download_cache(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync, post_to_record
    root=tmp_path/'archive'
    repo=ArchiveRepository.create(root,'10001','测试')
    repo.upsert_post(post_to_record(_sync_post('A')))
    sync=PersonalArchiveSync(root, _LiveSource([],{},None), _IdentityProvider())
    calls=[]
    def download(client,url,dest,**kw):
        if url.endswith('.ts'):calls.append(url)
        return _fake_download_file(client,url,dest,**kw)
    monkeypatch.setattr(sync_module,'download_file',download)
    original=sync_module.os.replace
    stopped=[False]
    def replace(source,dest,*args,**kwargs):
        result=original(source,dest,*args,**kwargs)
        if str(dest).endswith('.ts') and not stopped[0]:
            stopped[0]=True
            raise OperationPaused('发布时暂停')
        return result
    monkeypatch.setattr(sync_module.os,'replace',replace)
    with pytest.raises(OperationPaused):
        sync._download_live_replay(repo,'A','https://live.test/list.m3u8',('2026','09'))
    monkeypatch.setattr(sync_module.os,'replace',original)
    calls.clear()
    sync._download_live_replay(repo,'A','https://live.test/list.m3u8',('2026','09'))
    assert calls == []
    assert len(repo.list_media_for_render()) == 3
    repo.close()
