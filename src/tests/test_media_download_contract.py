"""媒体下载命名、恢复与取消契约。"""

from datetime import datetime
import json
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from weibo_book.errors import OperationCancelled, WeiboError, WeiboErrorKind
from weibo_book import media as media_service
from weibo_book.media import MediaDownloader, download_file
from weibo_book.post_converter import extract_media
from weibo_book.raw_status import RAW_STATUS_KEY
from weibo_book.models import MediaType, Post, PostMedia


def _post(media):
    return Post(
        bid="BID123",
        uid="1",
        user_name="测试",
        user_avatar="",
        text="",
        created_at=datetime(2026, 7, 14),
        media=media,
    )


def _write_success(_client, _url, dest, **_kwargs):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"ok")
    return True


def test_live_photo_keeps_same_base_and_actual_extensions(tmp_path):
    media = [
        PostMedia(type=MediaType.IMAGE, url="https://media.example/01.jpg"),
        PostMedia(type=MediaType.IMAGE, url="https://media.example/02.webp"),
        PostMedia(
            type=MediaType.LIVE_PHOTO,
            url="https://media.example/live.mov?token=1",
            thumbnail="https://media.example/live.heic?token=2",
        ),
    ]
    post = _post(media)
    downloader = MediaDownloader(tmp_path, max_workers=1)

    with patch("weibo_book.media.download_file", side_effect=_write_success):
        result = downloader.download_all([post])

    assert result == {"total": 4, "success": 4, "fail": 0, "failed": []}
    assert (tmp_path / "media" / "BID123_live_03.mov").read_bytes() == b"ok"
    assert (tmp_path / "media" / "BID123_live_03.heic").read_bytes() == b"ok"
    assert Path(media[2].local_path).name == "BID123_live_03.mov"
    assert Path(media[2].local_thumb).name == "BID123_live_03.heic"


def test_cancelled_download_stops_before_network(tmp_path):
    event = Event()
    event.set()
    downloader = MediaDownloader(tmp_path, max_workers=1)
    downloader._cancel_event = event
    post = _post([PostMedia(type=MediaType.IMAGE, url="https://media.example/01.jpg")])

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(OperationCancelled, match="任务已取消"):
            downloader.download_all([post])

    download.assert_not_called()


def test_failed_item_can_resume_on_next_run(tmp_path):
    media = [PostMedia(type=MediaType.VIDEO, url="https://media.example/01.mp4")]
    post = _post(media)
    downloader = MediaDownloader(tmp_path, max_workers=1)

    with patch("weibo_book.media.download_file", return_value=False):
        first = downloader.download_all([post])
    with patch("weibo_book.media.download_file", side_effect=_write_success):
        second = downloader.download_all([post])

    assert first["fail"] == 1
    assert second["success"] == 1
    assert (tmp_path / "media" / "BID123_video_01.mp4").exists()


def test_eighteen_item_fixture_downloads_every_resource_in_order(tmp_path):
    fixture_path = Path(__file__).parent / "fixtures" / "weibo_media_samples.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["eighteen_live_photo"]
    crawl_post = type("CrawlPost", (), {
        "raw_data": {RAW_STATUS_KEY: {"pics": fixture["pics"]}},
        "pic_urls": [],
        "video_url": "",
    })()
    post = _post(extract_media(crawl_post))
    downloader = MediaDownloader(tmp_path, max_workers=2)

    with patch("weibo_book.media.download_file", side_effect=_write_success):
        result = downloader.download_all([post])

    assert result["total"] == 22
    assert result["success"] == 22
    assert len(list((tmp_path / "media").iterdir())) == 22
    for position in (3, 14, 15, 17):
        assert (tmp_path / "media" / f"BID123_live_{position:02d}.jpg").exists()
        assert (tmp_path / "media" / f"BID123_live_{position:02d}.mov").exists()


def test_download_file_cancellation_removes_partial_file(tmp_path):
    event = Event()
    destination = tmp_path / "partial.mp4"

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"first"
            event.set()
            yield b"second"

    client = type("Client", (), {"stream": lambda self, *args, **kwargs: Response()})()

    with pytest.raises(OperationCancelled, match="任务已取消"):
        download_file(client, "https://media.example/large.mp4", destination, cancel_event=event)

    assert not destination.exists()


def test_download_file_failure_leaves_no_partial_destination_or_temp(tmp_path):
    destination = tmp_path / "new.jpg"

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"partial"
            raise OSError("注入的下载失败")

    client = type("Client", (), {"stream": lambda self, *args, **kwargs: Response()})()

    assert download_file(client, "https://media.example/new.jpg", destination, max_retries=0) is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_file_callbacks_cover_each_real_attempt_and_retry_wait(tmp_path):
    destination = tmp_path / "retry.jpg"
    calls = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"ok"

    class Client:
        attempts = 0

        def stream(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("注入的首次网络失败")
            return Response()

    assert download_file(
        Client(),
        "https://media.example/retry.jpg",
        destination,
        max_retries=1,
        before_request=lambda: calls.append("before"),
        request_completed=lambda: calls.append("completed"),
        retry_wait=lambda seconds: calls.append(("retry", seconds)),
    ) is True

    assert calls == [
        "before",
        "completed",
        ("retry", 1.0),
        "before",
        "completed",
    ]


def test_low_intensity_media_exact_432_is_rate_limit_error(tmp_path):
    class Response:
        status_code = 432

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    client = type("Client", (), {"stream": lambda self, *_args, **_kwargs: Response()})()

    with pytest.raises(WeiboError) as raised:
        download_file(
            client,
            "https://media.example/limited.jpg",
            tmp_path / "limited.jpg",
            max_retries=0,
            raise_request_errors=True,
        )

    assert raised.value.kind is WeiboErrorKind.RATE_LIMIT


@pytest.mark.parametrize(
    ("status_code", "headers", "chunks"),
    [
        (204, {"content-type": "image/jpeg"}, []),
        (200, {"content-type": "image/jpeg"}, []),
        (200, {"content-type": "text/html"}, [b"<html>login</html>"]),
        (200, {"content-type": "image/png"}, [b"png"]),
    ],
)
def test_image_download_rejects_non_image_empty_and_extension_conflict(
    tmp_path, status_code, headers, chunks
):
    destination = tmp_path / "comment.jpg"

    class Response:
        def __init__(self):
            self.status_code = status_code
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield from chunks

    client = type("Client", (), {"stream": lambda self, *args, **kwargs: Response()})()

    assert download_file(
        client,
        "https://media.example/comment.jpg",
        destination,
        max_retries=0,
        expected_image_extension="jpg",
    ) is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_image_download_replaces_only_after_response_context_exits(tmp_path):
    destination = tmp_path / "comment.jpg"

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise OSError("注入的 context exit 失败")

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"complete"

    client = type("Client", (), {"stream": lambda self, *args, **kwargs: Response()})()

    assert download_file(
        client,
        "https://media.example/comment.jpg",
        destination,
        max_retries=0,
        expected_image_extension="jpg",
    ) is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_image_download_accepts_matching_content_type_and_nonempty_body(tmp_path):
    destination = tmp_path / "comment.jpg"

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg; charset=binary"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    client = type("Client", (), {"stream": lambda self, *args, **kwargs: Response()})()

    assert download_file(
        client,
        "https://media.example/comment.jpg",
        destination,
        max_retries=0,
        expected_image_extension="jpg",
    ) is True
    assert destination.read_bytes() == b"image"


def test_download_referer_is_only_sent_to_exact_weibo_sina_hosts():
    assert media_service.download_headers_for_url("https://wx1.sinaimg.cn/large/a.jpg") == {"Referer": "https://weibo.com/"}
    assert media_service.download_headers_for_url("https://video.weibo.com/a.mp4") == {"Referer": "https://weibo.com/"}
    assert media_service.download_headers_for_url("https://i0.hdslb.com/bfs/a.jpg") == {}
    assert media_service.download_headers_for_url("https://weibo.com.evil.example/a.jpg") == {}
