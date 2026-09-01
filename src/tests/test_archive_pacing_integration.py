from __future__ import annotations

from datetime import datetime
from importlib.metadata import version as package_version
from pathlib import Path
from unittest.mock import patch

from crawl4weibo import WeiboClient
import pytest

from weibo_book.errors import OperationPaused, WeiboError, WeiboErrorKind
from weibo_book.archive.discovery import ProfilePage
from weibo_book.archive.source import ArchiveMediaStager, WeiboArchiveSource
from weibo_book.extractor import WeiboExtractor, _SingleAttemptWeiboClient
from weibo_book.media import MediaDownloader
from weibo_book.models import Comment, ImageQuality, LinkCard, MediaType, Post, PostMedia


class RecordingScheduler:
    mode = "low_2_3_hours"
    is_low_intensity = True

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.media_counts: list[int] = []

    def run(self, kind, operation):
        self.calls.append(kind)
        return operation()

    def before_request(self, kind):
        self.calls.append(kind)

    def record_completed_request(self, kind):
        self.calls.append(f"{kind}:done")

    def wait_for_retry(self, kind, seconds):
        self.calls.append(f"{kind}:retry:{seconds}")

    def add_media_requests(self, count):
        self.media_counts.append(count)


def _post(*, bid="BID1", media=None, comments=None, retweeted=None, link_card=None):
    return Post(
        bid=bid,
        uid="10001",
        user_name="本人",
        user_avatar="",
        text="",
        created_at=datetime(2026, 7, 18),
        media=list(media or []),
        comments=list(comments or []),
        retweeted=retweeted,
        link_card=link_card,
    )


def test_archive_source_routes_profile_detail_and_comments_through_scheduler():
    scheduler = RecordingScheduler()
    crawl_post = type("CrawlPost", (), {})()
    client = type(
        "Client",
        (),
        {
            "get_user_posts": lambda _self, *_args, **_kwargs: [],
            "get_post_by_bid": lambda _self, *_args, **_kwargs: crawl_post,
        },
    )()
    extractor = type("Extractor", (), {"client": client})()
    source = WeiboArchiveSource(
        extractor,
        self_uid="10001",
        image_quality=ImageQuality.ORIGINAL,
        pacing_scheduler=scheduler,
    )

    assert list(source.iter_profile_pages("10001")) == [ProfilePage([], is_last=True)]
    with patch("weibo_book.archive.source.crawl_post_to_our_post", return_value=_post()):
        source.fetch_post("10001", "BID1")
    with patch("weibo_book.archive.source.fetch_post_comments_strict", return_value=[]):
        source.fetch_recent_comments("MID1")

    assert scheduler.calls == ["profile", "detail", "comments"]


def test_low_intensity_media_count_comes_from_actual_unique_download_tasks(tmp_path):
    scheduler = RecordingScheduler()
    comment = Comment(
        id="C1",
        user_name="评论者",
        user_id="20002",
        user_avatar="",
        text="",
        created_at="2026-07-18 00:00:00",
        like_counts=0,
        image_url="https://media.example/comment.jpg",
    )
    retweeted = _post(
        bid="RET1",
        media=[PostMedia(type=MediaType.IMAGE, url="https://media.example/ret.jpg")],
    )
    post = _post(
        media=[
            PostMedia(type=MediaType.IMAGE, url="https://media.example/image.jpg"),
            PostMedia(
                type=MediaType.LIVE_PHOTO,
                url="https://media.example/live.mov",
                thumbnail="https://media.example/live.jpg",
            ),
            PostMedia(
                type=MediaType.VIDEO,
                url="https://media.example/video.mp4",
                thumbnail="https://media.example/cover.jpg",
            ),
        ],
        comments=[comment],
        retweeted=retweeted,
        link_card=LinkCard(
            type="webpage",
            url="https://example.com/",
            title="链接",
            description="",
            image_url="https://media.example/link.jpg",
        ),
    )
    downloader = MediaDownloader(
        tmp_path,
        max_workers=1,
        pacing_scheduler=scheduler,
    )

    request_options = []

    def write_success(_client, _url, destination, **kwargs):
        request_options.append(kwargs)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ok")
        return True

    with patch("weibo_book.media.download_file", side_effect=write_success):
        result = downloader.download_all([post])

    assert result["total"] == 8
    assert sum(scheduler.media_counts) == 8
    assert scheduler.calls.count("media") == 8
    assert all(options["max_retries"] == 0 for options in request_options)
    assert all(options["raise_request_errors"] is True for options in request_options)


def test_low_intensity_media_uses_one_worker_and_zero_count_without_media(tmp_path):
    scheduler = RecordingScheduler()
    stager = ArchiveMediaStager(
        image_quality=ImageQuality.ORIGINAL,
        pacing_scheduler=scheduler,
    )
    stager.stage(_post(), [], Path(tmp_path))

    assert scheduler.media_counts == [0]


def test_media_pause_stops_queued_low_intensity_requests(tmp_path):
    class PausingScheduler(RecordingScheduler):
        def run(self, kind, operation):
            self.calls.append(kind)
            try:
                return operation()
            except WeiboError as exc:
                if exc.kind is WeiboErrorKind.RATE_LIMIT:
                    raise OperationPaused("请求受到限流", pause_reason="rate_limited")
                raise

    scheduler = PausingScheduler()
    post = _post(media=[
        PostMedia(type=MediaType.IMAGE, url="https://media.example/first.jpg"),
        PostMedia(type=MediaType.IMAGE, url="https://media.example/second.jpg"),
    ])
    attempted = []

    def limited(_client, url, _destination, **_kwargs):
        attempted.append(url)
        raise WeiboError("精确 432", kind=WeiboErrorKind.RATE_LIMIT)

    downloader = MediaDownloader(tmp_path, max_workers=1, pacing_scheduler=scheduler)
    with patch("weibo_book.media.download_file", side_effect=limited):
        with pytest.raises(OperationPaused) as raised:
            downloader.download_all([post])

    assert raised.value.pause_reason == "rate_limited"
    assert len(attempted) == 1
    assert attempted[0].endswith("/first.jpg")
    assert scheduler.calls.count("media") == 1


def test_cached_media_is_not_added_to_real_request_count(tmp_path):
    scheduler = RecordingScheduler()
    post = _post(media=[
        PostMedia(type=MediaType.IMAGE, url="https://media.example/cached.jpg"),
        PostMedia(type=MediaType.IMAGE, url="https://media.example/new.jpg"),
    ])
    cached = tmp_path / "media" / "BID1_img_01.jpg"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")

    with patch("weibo_book.media.download_file", side_effect=lambda _client, _url, dest, **_kwargs: (dest.write_bytes(b"new") or True)):
        MediaDownloader(tmp_path, max_workers=1, pacing_scheduler=scheduler).download_all([post])

    assert sum(scheduler.media_counts) == 1
    assert scheduler.calls.count("media") == 1


def test_all_media_are_registered_before_first_low_intensity_request(tmp_path):
    class CountAtRequestScheduler(RecordingScheduler):
        def __init__(self):
            super().__init__()
            self.counts_at_request = []

        def run(self, kind, operation):
            self.counts_at_request.append(sum(self.media_counts))
            return super().run(kind, operation)

    scheduler = CountAtRequestScheduler()
    post = _post(media=[
        PostMedia(type=MediaType.VIDEO, url=f"https://media.example/{index}.mp4")
        for index in range(3)
    ])

    def write(_client, _url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ok")
        return True

    with patch("weibo_book.media.download_file", side_effect=write):
        MediaDownloader(tmp_path, max_workers=1, pacing_scheduler=scheduler).download_all([post])

    assert scheduler.media_counts == [3]
    assert scheduler.counts_at_request == [3, 3, 3]


def test_avatar_request_is_added_to_media_count(tmp_path):
    scheduler = RecordingScheduler()
    downloader = MediaDownloader(tmp_path, max_workers=1, pacing_scheduler=scheduler)

    def fake_download(_client, _url, display_dest, _directory_fd, destination_name, **_kwargs):
        target = display_dest.parent / f"{destination_name}.jpg"
        target.write_bytes(b"avatar")
        return target.name

    with patch("weibo_book.media._download_file_at", side_effect=fake_download):
        path = downloader.download_avatar("https://media.example/avatar.jpg", "user:10001")

    assert path is not None
    assert sum(scheduler.media_counts) == 1
    assert scheduler.calls.count("media") == 1


def test_low_intensity_client_contract_is_locked_to_crawl4weibo_0_5_2():
    assert package_version("crawl4weibo") == "0.5.2"
    extractor = WeiboExtractor(cookie_str="SUB=redacted", low_intensity=True)
    assert extractor.client._client_class is _SingleAttemptWeiboClient
    assert extractor.client._client_options["rate_limit_config"].disable_delay is True


def test_low_intensity_client_forces_parent_request_to_one_attempt():
    client = object.__new__(_SingleAttemptWeiboClient)
    with patch.object(WeiboClient, "_request", return_value={"ok": 1}) as request:
        assert client._request("https://example.test", {}, max_retries=99) == {"ok": 1}

    request.assert_called_once_with(
        "https://example.test",
        {},
        max_retries=1,
        use_proxy=True,
        headers=None,
    )


def test_standard_extractor_keeps_existing_default_client_options():
    extractor = WeiboExtractor(cookie_str="SUB=redacted")
    assert extractor.client._client_options == {}
