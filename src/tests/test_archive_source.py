"""生产归档来源与媒体暂存适配器。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from crawl4weibo.models.post import Post as CrawlPost
from crawl4weibo.exceptions.base import ParseError

from weibo_book.archive.source import ArchiveMediaStager, WeiboArchiveSource
from weibo_book.models import Comment, ImageQuality, MediaType, Post, PostMedia
from weibo_book.media import MediaDownloader
from weibo_book.errors import WeiboError, WeiboErrorKind, classify_error


def _crawl_post(bid: str, uid: str = "10001") -> CrawlPost:
    return CrawlPost(
        id=bid,
        bid=bid,
        user_id=uid,
        text=f"正文 {bid}",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
        pic_urls=[f"https://example.test/{bid}.jpg"],
        raw_data={"user": {"screen_name": "本人", "profile_image_url": "avatar"}},
    )


class FakeClient:
    def __init__(self):
        self.pages = {1: [_crawl_post("A"), _crawl_post("B")], 2: []}
        self.post = _crawl_post("A")
        self.profile_calls = []
        self.post_calls = []

    def get_user_posts(self, uid, page, expand, with_comments):
        self.profile_calls.append((uid, page, expand, with_comments))
        return self.pages[page]

    def get_post_by_bid(self, bid, with_comments):
        self.post_calls.append((bid, with_comments))
        return self.post

    def get_comments(self, post_id, page=1):
        return [], None


class FakeExtractor:
    def __init__(self):
        self.client = FakeClient()
        self.comment_calls = []

    def get_post_comments(self, post_id, blogger_uid, count, comments_type):
        self.comment_calls.append((post_id, blogger_uid, count, comments_type))
        return [
            Comment("c1", "评论", "评论者", "2", "", "", 0)
        ]


def test_source_uses_existing_client_converter_and_comment_fetcher():
    extractor = FakeExtractor()
    source = WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL
    )

    pages = list(source.iter_profile_pages("10001"))
    post = source.fetch_post("10001", "A")
    comments = source.fetch_recent_comments("raw-A", limit=10)

    assert [[item.bid for item in page.items] for page in pages] == [["A", "B"], []]
    assert pages[0].is_last is False
    assert pages[1].is_last is True
    assert extractor.client.profile_calls == [
        ("10001", 1, False, False),
        ("10001", 2, False, False),
    ]
    assert extractor.client.post_calls == [("A", False)]
    assert post.bid == "A"
    assert post.uid == "10001"
    assert post.user_name == "本人"
    assert comments == []


def test_media_stager_downloads_only_into_work_and_returns_records(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        created_at=datetime(2026, 7, 14, 16, 38, tzinfo=timezone.utc),
        media=[PostMedia(MediaType.IMAGE, "https://example.test/A.jpg")],
        retweeted=Post(
            bid="R", uid="20002", user_name="原作者", user_avatar="", text="r",
            created_at=datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc),
            media=[PostMedia(MediaType.IMAGE, "https://example.test/R.jpg")],
        ),
    )
    comment = Comment(
        "c1", "评论", "用户", "2", "", "", 0,
        image_url="https://example.test/c1.jpg",
    )
    created_roots = []

    class FakeDownloader:
        def __init__(self, root, image_quality):
            self.root = Path(root)
            self._cancel_event = None
            created_roots.append(self.root)

        def download_all(self, posts):
            value = posts[0]
            media_path = self.root / "media" / "A_img_01.jpg"
            comment_path = self.root / "media" / "comments" / "A_c1.jpg"
            media_path.parent.mkdir(parents=True)
            comment_path.parent.mkdir(parents=True)
            media_path.write_bytes(b"post")
            comment_path.write_bytes(b"comment")
            value.media[0].local_path = str(media_path)
            value.comments[0].local_image = "media/comments/A_c1.jpg"
            retweeted_path = self.root / "media" / "R_img_01.jpg"
            retweeted_path.write_bytes(b"retweeted")
            value.retweeted.media[0].local_path = str(retweeted_path)
            return {"total": 3, "success": 3, "fail": 0, "failed": []}

    work_root = tmp_path / "archive" / ".work" / "run"
    stager = ArchiveMediaStager(
        image_quality=ImageQuality.ORIGINAL, downloader_factory=FakeDownloader
    )
    staged = stager.stage(post, [comment], work_root)

    assert created_roots == [work_root]
    paths = {item.record.local_path for item in staged}
    assert "media/comments/2026/07/A_c1.jpg" in paths
    assert any(path.startswith("media/posts/2026/07/A_image_0_") for path in paths)
    assert any(path.startswith("media/posts/2020/01/R_image_0_") for path in paths)
    assert all(item.staged_path.is_relative_to(work_root) for item in staged)
    assert all(item.staged_path.is_file() for item in staged)
    assert post.media[0].local_path.startswith("media/posts/2026/07/A_image_0_")
    assert comment.local_image == "media/comments/2026/07/A_c1.jpg"


def test_media_stager_localizes_main_retweet_and_comment_avatars(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="https://img.test/main.png", text="x",
        retweeted=Post(bid="R", uid="20002", user_name="原作者", user_avatar="https://img.test/retweet.png", text="r"),
    )
    comment = Comment("c1", "评论", "用户", "30003", "https://img.test/comment.png", "", 0)

    class FakeDownloader:
        def __init__(self, root, image_quality): self.root=Path(root); self._cancel_event=None
        def download_all(self, posts): return {"total":0,"success":0,"fail":0,"failed":[]}
        def download_avatar(self, url, identity):
            path=self.root/"media"/"avatars"/f"{identity.replace(':','-')}.png"
            path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(url.encode()); return path

    staged = ArchiveMediaStager(
        image_quality=ImageQuality.ORIGINAL, downloader_factory=FakeDownloader
    ).stage(post, [comment], tmp_path / "work")

    assert {(item.record.owner_type,item.record.owner_id,item.record.role) for item in staged} == {
        ("user","10001","avatar"),("retweeted_user","20002","avatar"),("comment","30003","avatar")
    }
    assert all(item.record.local_path.startswith("media/avatars/") for item in staged)


def test_avatar_url_change_creates_new_content_addressed_path_and_retains_old(tmp_path, monkeypatch):
    import weibo_book.media as media_module

    calls = []

    def _write_at(directory_reference, name: str, payload: bytes) -> None:
        if isinstance(directory_reference, int):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_reference,
            )
        else:
            target = media_module._checked_directory_path(directory_reference) / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)

    def fake_download(_client, url, _display, directory_reference, destination_name, **_kwargs):
        calls.append(url)
        name = f"{destination_name}.png"
        _write_at(directory_reference, name, url.encode("utf-8"))
        return name

    monkeypatch.setattr(media_module, "_download_file_at", fake_download)
    downloader = MediaDownloader(tmp_path, max_workers=1)
    first = downloader.download_avatar("https://img.test/avatar-v1.png", "user:10001")
    repeated = downloader.download_avatar("https://img.test/avatar-v1.png", "user:10001")
    second = downloader.download_avatar("https://img.test/avatar-v2.png", "user:10001")

    assert first == repeated
    assert first != second
    assert first.is_file() and second.is_file()
    assert calls == ["https://img.test/avatar-v1.png", "https://img.test/avatar-v2.png"]


def test_exact_crawl_post_not_found_is_converted_to_not_found():
    assert classify_error(ParseError("Post BID123 not found")) is WeiboErrorKind.NOT_FOUND
    assert classify_error(ParseError("Other value not found")) is WeiboErrorKind.PARSE
    extractor = FakeExtractor()
    extractor.client.get_post_by_bid = lambda bid, with_comments: (_ for _ in ()).throw(
        ParseError(f"Post {bid} not found")
    )
    source = WeiboArchiveSource(extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL)
    try:
        source.fetch_post("10001", "BID123")
    except WeiboError as exc:
        assert exc.kind is WeiboErrorKind.NOT_FOUND
    else:
        raise AssertionError("应转换为 WeiboError")


def test_source_numbers_pins_across_pages_without_renumbering_duplicates(monkeypatch):
    extractor = FakeExtractor()
    extractor.client.pages = {
        1: [_crawl_post("P1"), _crawl_post("N")],
        2: [_crawl_post("P1"), _crawl_post("P2")],
        3: [],
    }

    def convert(value, uid, image_quality):
        return Post(
            bid=value.bid, uid=uid, user_name="", user_avatar="", text="",
            is_pinned=value.bid.startswith("P"),
        )

    monkeypatch.setattr("weibo_book.archive.source.crawl_post_to_our_post", convert)
    pages = list(WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL
    ).iter_profile_pages("10001"))
    observed = [
        (item.bid, item.is_pinned, item.pin_order)
        for page in pages for item in page.items
    ]
    assert observed == [
        ("P1", True, 1), ("N", False, None),
        ("P1", True, 1), ("P2", True, 2),
    ]


def test_target_mode_allows_target_and_rejects_other_uids():
    extractor = FakeExtractor()
    source = WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL,
        target_uid="20002",
    )

    pages = list(source.iter_profile_pages("20002"))
    assert [[item.bid for item in page.items] for page in pages] == [["A", "B"], []]
    assert extractor.client.profile_calls[0][0] == "20002"

    for blocked in ("10001", "30003"):
        try:
            list(source.iter_profile_pages(blocked))
        except WeiboError as exc:
            assert exc.kind is WeiboErrorKind.AUTH
        else:
            raise AssertionError("非目标 UID 必须被拒绝")


def test_target_mode_skips_comment_fetching():
    extractor = FakeExtractor()
    source = WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL,
        target_uid="20002",
    )
    assert source.fetch_recent_comments("raw-A", limit=10) == []
    assert extractor.comment_calls == []


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, timeout):
        return _FakeResponse(self._payload)


def test_target_mode_probe_session_accepts_any_logged_in_account():
    extractor = FakeExtractor()
    extractor.client.session = _FakeSession({"data": {"login": True, "uid": "99999"}})
    source = WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL,
        target_uid="20002",
    )
    # 他人模式：登录账号与目标不同也允许，只要求登录态有效
    source.probe_session()


def test_self_mode_probe_session_still_rejects_account_mismatch():
    from weibo_book.errors import OperationPaused

    extractor = FakeExtractor()
    extractor.client.session = _FakeSession({"data": {"login": True, "uid": "99999"}})
    source = WeiboArchiveSource(
        extractor, self_uid="10001", image_quality=ImageQuality.ORIGINAL
    )
    try:
        source.probe_session()
    except OperationPaused as exc:
        assert exc.pause_reason == "account_mismatch"
    else:
        raise AssertionError("本人模式登录账号不一致必须暂停")
