"""本人微博归档的 IP、置顶、评论图片与回复关联契约。"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import islice
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from crawl4weibo.models.comment import Comment as CrawlComment
from crawl4weibo.models.post import Post as CrawlPost

from tests.symlink_capability import require_symlink_capability
from weibo_book.comment_fetcher import convert_crawl_comment, fetch_post_comments
from weibo_book.errors import WeiboError, WeiboErrorKind
from weibo_book.media import MediaDownloader
from weibo_book.models import Comment, Post
from weibo_book.post_converter import crawl_post_to_our_post
from weibo_book.raw_status import RAW_STATUS_KEY


def _crawl_comment(
    comment_id: str,
    *,
    created_at: str = "",
    user_id: str = "other",
    reply_id: str | None = None,
) -> CrawlComment:
    return CrawlComment(
        id=comment_id,
        text=f"评论 {comment_id}",
        created_at=created_at,
        user_id=user_id,
        user_screen_name=f"用户 {comment_id}",
        reply_id=reply_id,
    )


class _CommentClient:
    def __init__(self, comments: list[CrawlComment]) -> None:
        self.comments = comments

    def get_comments(self, _post_id, page=1):
        assert page == 1
        return list(self.comments), None

    def get_all_comments(self, _post_id, max_pages=1):
        assert max_pages >= 1
        return list(self.comments)


def _post(*, comments: list[Comment]) -> Post:
    return Post(
        bid="BID123",
        uid="owner",
        user_name="本人",
        user_avatar="",
        text="",
        created_at=datetime(2026, 7, 14),
        comments=comments,
    )


def test_comment_conversion_keeps_exact_crawl_fields():
    crawl = CrawlComment(
        id="c2",
        text="回复内容",
        source="来自 iPhone",
        user_id="u2",
        user_screen_name="评论者",
        user_avatar_url="avatar.jpg",
        created_at="2026-07-14 00:40",
        like_counts=3,
        reply_id="c1",
        reply_text="原评论",
        pic_url="https://wx1.sinaimg.cn/large/c2.jpg",
    )

    comment = convert_crawl_comment(crawl, blogger_uid="owner")

    assert comment.source == "来自 iPhone"
    assert comment.image_url == "https://wx1.sinaimg.cn/large/c2.jpg"
    assert comment.parent_id == "c1"
    assert comment.reply_to_name == ""
    assert comment.replies == []


def test_comment_conversion_keeps_blogger_identity_semantics_strict():
    commenter = convert_crawl_comment(
        _crawl_comment("c1", user_id="commenter"), blogger_uid="owner"
    )
    owner = convert_crawl_comment(
        _crawl_comment("c2", user_id="owner"), blogger_uid="owner"
    )

    assert commenter.is_blogger is False
    assert owner.is_blogger is True


def test_post_converter_keeps_fixture_verified_ip_and_pin():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/self_archive/post_cards.json").read_text(
            encoding="utf-8"
        )
    )
    raw = fixture["pinned_card"]["mblog"]
    crawl = CrawlPost(
        id="1",
        bid=raw["bid"],
        user_id="owner",
        raw_data={RAW_STATUS_KEY: raw},
    )

    post = crawl_post_to_our_post(crawl, "owner")

    assert post.ip_location == "发布于 测试地区"
    assert post.is_pinned is True
    assert post.pin_order is None


def test_post_converter_does_not_guess_similar_or_case_changed_fields():
    crawl = CrawlPost(
        id="1",
        bid="BID123",
        user_id="owner",
        raw_data={
            RAW_STATUS_KEY: {
                "Region_Name": "不得读取",
                "Title": {"text": "置顶"},
                "title": {"Text": "置顶"},
            }
        },
    )

    post = crawl_post_to_our_post(crawl, "owner")

    assert post.ip_location == ""
    assert post.is_pinned is False


def test_post_converter_ignores_verified_fields_from_raw_data_top_level():
    crawl = CrawlPost(
        id="1",
        bid="BID123",
        user_id="owner",
        raw_data={
            "region_name": "发布于 不得读取的顶层",
            "title": {"text": "置顶"},
        },
    )

    post = crawl_post_to_our_post(crawl, "owner")

    assert post.ip_location == ""
    assert post.is_pinned is False


def test_recent_comments_sort_parsed_times_desc_and_limit_ten():
    start = datetime(2026, 7, 14, 0, 0)
    comments = [
        _crawl_comment(
            f"c{index:02d}",
            created_at=(start + timedelta(minutes=index)).isoformat(sep=" "),
        )
        for index in range(12)
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=20, comments_type="all"
    )

    assert [comment.id for comment in result] == [
        "c11", "c10", "c09", "c08", "c07", "c06", "c05", "c04", "c03", "c02"
    ]


def test_all_comments_uses_exactly_one_page_even_when_count_exceeds_twenty():
    class PagedClient:
        def __init__(self):
            self.max_pages_calls = []

        def get_all_comments(self, _post_id, max_pages=1):
            self.max_pages_calls.append(max_pages)
            first_page = [
                _crawl_comment("first-parent", created_at="2026-07-14 01:00:00")
            ]
            second_page = [
                _crawl_comment("second-parent", created_at="2026-07-14 03:00:00"),
                _crawl_comment("cross-page-reply", reply_id="first-parent"),
            ]
            return first_page + (second_page if max_pages > 1 else [])

    client = PagedClient()

    result = fetch_post_comments(
        client, "post", "owner", count=50, comments_type="all"
    )

    assert client.max_pages_calls == [1]
    assert [comment.id for comment in result] == ["first-parent"]
    assert result[0].replies == []


def test_unparseable_parent_times_keep_api_relative_order_and_log(caplog):
    comments = [
        _crawl_comment("bad-1", created_at="不可解析-1"),
        _crawl_comment("new", created_at="2026-07-14 02:00:00"),
        _crawl_comment("bad-2", created_at="不可解析-2"),
        _crawl_comment("old", created_at="2026-07-14 01:00:00"),
    ]

    with caplog.at_level("DEBUG", logger="weibo_book.comment_fetcher"):
        result = fetch_post_comments(
            _CommentClient(comments), "post", "owner", count=10, comments_type="all"
        )

    assert [comment.id for comment in result] == ["bad-1", "new", "bad-2", "old"]
    assert "无法解析评论时间" in caplog.text


def test_mixed_timezone_and_local_comment_times_keep_api_order_and_log(caplog):
    comments = [
        _crawl_comment("zoned", created_at="Tue Jul 14 02:00:00 +0800 2026"),
        _crawl_comment("local", created_at="2026-07-14 03:00:00"),
    ]

    with caplog.at_level("DEBUG", logger="weibo_book.comment_fetcher"):
        result = fetch_post_comments(
            _CommentClient(comments), "post", "owner", count=10, comments_type="all"
        )

    assert [comment.id for comment in result] == ["zoned", "local"]
    assert "时区" in caplog.text


def test_aware_comment_times_sort_by_utc_instant_not_wall_clock():
    comments = [
        _crawl_comment("utc-new", created_at="2026-07-14 03:00:00 +0000"),
        _crawl_comment("beijing-old", created_at="2026-07-14 10:00:00 +0800"),
        _crawl_comment("utc-equivalent", created_at="2026-07-14 02:00:00 +0000"),
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=10, comments_type="all"
    )

    assert [comment.id for comment in result] == [
        "utc-new", "beijing-old", "utc-equivalent"
    ]


def test_one_unparseable_time_keeps_entire_page_order_before_limit(caplog):
    comments = [_crawl_comment("just-now", created_at="刚刚")]
    comments.extend(
        _crawl_comment(
            f"c{index:02d}", created_at=f"2026-07-14 {index:02d}:00:00"
        )
        for index in range(10)
    )

    with caplog.at_level("DEBUG", logger="weibo_book.comment_fetcher"):
        result = fetch_post_comments(
            _CommentClient(comments), "post", "owner", count=20, comments_type="all"
        )

    assert [comment.id for comment in result] == [
        "just-now", "c00", "c01", "c02", "c03", "c04", "c05", "c06", "c07", "c08"
    ]
    assert "整页" in caplog.text


def test_replies_attach_to_retained_parent_without_using_parent_limit():
    comments = [
        _crawl_comment("parent", created_at="2026-07-14 02:00:00"),
        _crawl_comment("reply-1", created_at="2026-07-14 02:01:00", reply_id="parent"),
        _crawl_comment("reply-2", created_at="2026-07-14 02:02:00", reply_id="parent"),
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=1, comments_type="all"
    )

    assert [comment.id for comment in result] == ["parent"]
    assert [reply.id for reply in result[0].replies] == ["reply-1", "reply-2"]


def test_orphan_reply_and_reply_for_unretained_parent_are_not_misattached():
    comments = [
        _crawl_comment("kept", created_at="2026-07-14 03:00:00"),
        _crawl_comment("dropped", created_at="2026-07-14 01:00:00"),
        _crawl_comment("orphan", reply_id="missing"),
        _crawl_comment("dropped-reply", reply_id="dropped"),
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=1, comments_type="all"
    )

    assert [comment.id for comment in result] == ["kept"]
    assert result[0].replies == []


def test_blogger_reply_keeps_non_blogger_parent_as_context():
    comments = [
        _crawl_comment("context", user_id="other", created_at="2026-07-14 01:00:00"),
        _crawl_comment("owner-reply", user_id="owner", reply_id="context"),
        _crawl_comment("other-parent", user_id="other", created_at="2026-07-14 02:00:00"),
        _crawl_comment("other-reply", user_id="other", reply_id="other-parent"),
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=10, comments_type="blogger"
    )

    assert [comment.id for comment in result] == ["context"]
    assert [reply.id for reply in result[0].replies] == ["owner-reply"]


def test_duplicate_comment_ids_and_self_reply_do_not_duplicate_or_self_attach():
    comments = [
        _crawl_comment("same", created_at="2026-07-14 01:00:00"),
        _crawl_comment("same", created_at="2026-07-14 02:00:00"),
        _crawl_comment("self", reply_id="self"),
    ]

    result = fetch_post_comments(
        _CommentClient(comments), "post", "owner", count=10, comments_type="all"
    )

    assert [comment.id for comment in result] == ["same"]


def test_comment_image_existing_nonempty_file_is_reused_without_network(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg?token=secret",
    )
    destination = tmp_path / "media/comments/BID123_c1.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    with patch("weibo_book.media.download_file") as download:
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    download.assert_not_called()
    assert result["success"] == 1
    assert comment.local_image == "media/comments/BID123_c1.jpg"


def test_comment_image_success_uses_stable_posix_relative_path(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.webp?token=secret",
    )

    def write_success(_client, _url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"image")
        return True

    with patch("weibo_book.media.download_file", side_effect=write_success):
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert result["success"] == 1
    assert comment.local_image == "media/comments/BID123_c1.webp"
    assert (tmp_path / comment.local_image).read_bytes() == b"image"
    assert "?" not in comment.local_image


def test_comment_image_failure_preserves_old_local_image(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
        local_image="media/comments/old-c1.jpg",
    )

    with patch("weibo_book.media.download_file", return_value=False):
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert result["fail"] == 1
    assert comment.local_image == "media/comments/old-c1.jpg"


@pytest.mark.parametrize("post_bid,comment_id", [("../post", "c1"), ("post", "../c1"), ("post", "a/b")])
def test_comment_image_rejects_unsafe_path_identifiers(tmp_path, post_bid, comment_id):
    comment = Comment(
        comment_id, "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
    )
    post = _post(comments=[comment])
    post.bid = post_bid

    with pytest.raises(WeiboError) as exc_info:
        MediaDownloader(tmp_path, max_workers=1).download_all([post])

    assert exc_info.value.kind is WeiboErrorKind.PARSE
    assert "不安全" in str(exc_info.value)


def test_comment_image_rejects_url_path_traversal(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/../secret.jpg",
    )

    with pytest.raises(WeiboError) as exc_info:
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert exc_info.value.kind is WeiboErrorKind.PARSE
    assert "URL 不安全" in str(exc_info.value)


def test_failed_comment_download_removes_preexisting_empty_file(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
    )
    destination = tmp_path / "media/comments/BID123_c1.jpg"
    destination.parent.mkdir(parents=True)
    destination.touch()

    with patch("weibo_book.media.download_file", return_value=False):
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert not destination.exists()


def test_failed_comment_download_does_not_backfill_from_residual_file(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
    )

    def fail_with_residue(_client, _url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"residue")
        return False

    with patch("weibo_book.media.download_file", side_effect=fail_with_residue):
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert comment.local_image is None


def test_duplicate_comment_path_same_url_downloads_once_and_backfills_all(tmp_path):
    first = Comment(
        "same", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/same.jpg",
    )
    second = Comment(
        "same", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/same.jpg",
    )

    with patch("weibo_book.media.download_file", return_value=True) as download:
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[first, second])]
        )

    assert download.call_count == 1
    assert first.local_image == "media/comments/BID123_same.jpg"
    assert second.local_image == "media/comments/BID123_same.jpg"


def test_duplicate_comment_path_different_url_is_rejected(tmp_path):
    comments = [
        Comment("same", "", "", "", "", "", 0, image_url="https://wx1.sinaimg.cn/large/a.jpg"),
        Comment("same", "", "", "", "", "", 0, image_url="https://wx2.sinaimg.cn/large/b.jpg"),
    ]

    with pytest.raises(WeiboError, match="冲突") as exc_info:
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=comments)]
        )

    assert exc_info.value.kind is WeiboErrorKind.PARSE


def test_comment_iterator_stops_self_and_two_object_cycles():
    self_cycle = Comment("self", "", "", "", "", "", 0)
    self_cycle.replies.append(self_cycle)
    first = Comment("first", "", "", "", "", "", 0)
    second = Comment("second", "", "", "", "", "", 0)
    first.replies.append(second)
    second.replies.append(first)

    assert list(islice(MediaDownloader._iter_comments([self_cycle]), 5)) == [self_cycle]
    assert list(islice(MediaDownloader._iter_comments([first]), 5)) == [first, second]


def test_comment_target_symlink_is_rejected_without_network(tmp_path):
    require_symlink_capability(target_is_directory=False)
    comment = Comment("c1", "", "", "", "", "", 0, image_url="https://wx1.sinaimg.cn/large/c1.jpg")
    external = tmp_path / "external.jpg"
    external.write_bytes(b"outside")
    destination = tmp_path / "media/comments/BID123_c1.jpg"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(external)

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(WeiboError, match="符号链接"):
            MediaDownloader(tmp_path, max_workers=1).download_all([_post(comments=[comment])])

    download.assert_not_called()
    assert external.read_bytes() == b"outside"


def test_comment_directory_symlink_is_rejected_without_network(tmp_path):
    require_symlink_capability(target_is_directory=True)
    comment = Comment("c1", "", "", "", "", "", 0, image_url="https://wx1.sinaimg.cn/large/c1.jpg")
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "media/comments").symlink_to(external, target_is_directory=True)

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(WeiboError, match="符号链接"):
            MediaDownloader(tmp_path, max_workers=1).download_all([_post(comments=[comment])])

    download.assert_not_called()
    assert list(external.iterdir()) == []


def test_comment_target_hardlink_is_rejected(tmp_path):
    comment = Comment("c1", "", "", "", "", "", 0, image_url="https://wx1.sinaimg.cn/large/c1.jpg")
    external = tmp_path / "external.jpg"
    external.write_bytes(b"outside")
    destination = tmp_path / "media/comments/BID123_c1.jpg"
    destination.parent.mkdir(parents=True)
    os.link(external, destination)

    with pytest.raises(WeiboError, match="硬链接"):
        MediaDownloader(tmp_path, max_workers=1).download_all([_post(comments=[comment])])

    assert external.read_bytes() == b"outside"


def test_comment_directory_swap_race_never_writes_external_target(tmp_path):
    require_symlink_capability(target_is_directory=True)
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
    )
    comments_dir = tmp_path / "media/comments"
    held_dir = tmp_path / "media/comments-held"
    external = tmp_path / "external"
    external.mkdir()

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            comments_dir.rename(held_dir)
            comments_dir.symlink_to(external, target_is_directory=True)
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    from weibo_book.media import _SUPPORTS_DIRECTORY_FDS

    with patch("weibo_book.media.httpx.Client", Client):
        if _SUPPORTS_DIRECTORY_FDS:
            result = MediaDownloader(tmp_path, max_workers=1).download_all(
                [_post(comments=[comment])]
            )
        else:
            with pytest.raises(WeiboError, match="评论图片目录在操作时已变化"):
                MediaDownloader(tmp_path, max_workers=1).download_all(
                    [_post(comments=[comment])]
                )

    if _SUPPORTS_DIRECTORY_FDS:
        assert result["success"] == 1
    assert list(external.iterdir()) == []
    if _SUPPORTS_DIRECTORY_FDS:
        assert (held_dir / "BID123_c1.jpg").read_bytes() == b"image"
    else:
        assert not (held_dir / "BID123_c1.jpg").exists()


def test_comment_directory_swap_race_checked_branch_raises_when_swap_succeeds(
    tmp_path, monkeypatch
):
    """强制 _CheckedDirectory 分支：目录被换成符号链接后必须抛错。

    真实 Windows 曾出现"未抛错"：当 swap 本身失败（改名权限/链接权限）
    时生产按普通异常重试并成功，属于合法路径；本用例锁定的是
    swap 成功后校验必须失败关闭，与平台无关。
    """
    import weibo_book.media as media_module

    require_symlink_capability(target_is_directory=True)
    monkeypatch.setattr(media_module, "_SUPPORTS_DIRECTORY_FDS", False)
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/c1.jpg",
    )
    comments_dir = tmp_path / "media/comments"
    held_dir = tmp_path / "media/comments-held"
    external = tmp_path / "external"
    external.mkdir()

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            comments_dir.rename(held_dir)
            comments_dir.symlink_to(external, target_is_directory=True)
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    with patch("weibo_book.media.httpx.Client", Client):
        with pytest.raises(WeiboError, match="评论图片目录在操作时已变化"):
            MediaDownloader(tmp_path, max_workers=1).download_all(
                [_post(comments=[comment])]
            )

    assert list(external.iterdir()) == []
    assert not (held_dir / "BID123_c1.jpg").exists()


@pytest.mark.parametrize(
    ("content_type", "expected_extension"),
    [
        ("image/jpeg", "jpg"),
        ("image/png", "png"),
        ("image/webp", "webp"),
    ],
)
def test_extensionless_comment_image_uses_verified_mime_extension(
    tmp_path, content_type, expected_extension
):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource?token=secret",
    )

    class Response:
        status_code = 200
        headers = {"content-type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    with patch("weibo_book.media.httpx.Client", Client):
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    expected_relative = f"media/comments/BID123_c1.{expected_extension}"
    assert result["success"] == 1
    assert comment.local_image == expected_relative
    assert (tmp_path / expected_relative).read_bytes() == b"image"


def test_extensionless_comment_image_rejects_unknown_image_mime_and_keeps_old(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
        local_image="media/comments/old-c1.jpg",
    )

    class Response:
        status_code = 200
        headers = {"content-type": "image/bmp"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"bitmap"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Response()

    with patch("weibo_book.media.httpx.Client", Client):
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert result["fail"] == 1
    assert comment.local_image == "media/comments/old-c1.jpg"
    assert list((tmp_path / "media/comments").iterdir()) == []


def test_duplicate_extensionless_comment_url_downloads_once_and_backfills_all(tmp_path):
    url = "https://wx1.sinaimg.cn/large/opaque-resource"
    first = Comment("same", "", "", "", "", "", 0, image_url=url)
    second = Comment("same", "", "", "", "", "", 0, image_url=url)

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    class Client:
        stream_calls = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            type(self).stream_calls += 1
            return Response()

    with patch("weibo_book.media.httpx.Client", Client):
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[first, second])]
        )

    assert Client.stream_calls == 1
    assert first.local_image == "media/comments/BID123_same.jpg"
    assert second.local_image == "media/comments/BID123_same.jpg"


def test_extensionless_comment_second_run_reuses_verified_local_path_without_network(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
    )

    class Response:
        status_code = 200
        headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"image"

    class Client:
        stream_calls = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            type(self).stream_calls += 1
            return Response()

    downloader = MediaDownloader(tmp_path, max_workers=1)
    with patch("weibo_book.media.httpx.Client", Client):
        downloader.download_all([_post(comments=[comment])])
        downloader.download_all([_post(comments=[comment])])

    assert Client.stream_calls == 1
    assert comment.local_image == "media/comments/BID123_c1.png"


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_extensionless_old_local_image_rejects_unsafe_target(tmp_path, link_type):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
        local_image="media/comments/BID123_c1.png",
    )
    external = tmp_path / "external.png"
    external.write_bytes(b"outside")
    target = tmp_path / comment.local_image
    target.parent.mkdir(parents=True)
    if link_type == "symlink":
        require_symlink_capability(target_is_directory=False)
        target.symlink_to(external)
        message = "符号链接"
    else:
        os.link(external, target)
        message = "硬链接"

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(WeiboError, match=message):
            MediaDownloader(tmp_path, max_workers=1).download_all(
                [_post(comments=[comment])]
            )

    download.assert_not_called()
    assert external.read_bytes() == b"outside"


def test_extensionless_scan_rejects_multiple_allowed_extensions(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
    )
    comments_dir = tmp_path / "media/comments"
    comments_dir.mkdir(parents=True)
    (comments_dir / "BID123_c1.jpg").write_bytes(b"jpg")
    (comments_dir / "BID123_c1.png").write_bytes(b"png")

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(WeiboError, match="多个扩展名.*冲突"):
            MediaDownloader(tmp_path, max_workers=1).download_all(
                [_post(comments=[comment])]
            )

    download.assert_not_called()


def test_extensionless_scan_reuses_unique_safe_allowed_extension(tmp_path):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
    )
    target = tmp_path / "media/comments/BID123_c1.webp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"webp")

    with patch("weibo_book.media.download_file") as download:
        result = MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    download.assert_not_called()
    assert result["success"] == 1
    assert comment.local_image == "media/comments/BID123_c1.webp"


@pytest.mark.parametrize(
    "old_local_image",
    [
        "media/comments/WRONG_c1.png",
        "media/comments/BID123_other.png",
        "media/comments/BID123_c1.bmp",
        "media\\comments\\BID123_c1.png",
    ],
)
def test_extensionless_wrong_old_basename_is_not_reused(
    tmp_path, old_local_image
):
    comment = Comment(
        "c1", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/opaque-resource",
        local_image=old_local_image,
    )
    if "\\" not in old_local_image:
        wrong_target = tmp_path / old_local_image
        wrong_target.parent.mkdir(parents=True, exist_ok=True)
        wrong_target.write_bytes(b"wrong")

    class Response:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield b"correct"

    class Client:
        stream_calls = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            type(self).stream_calls += 1
            return Response()

    with patch("weibo_book.media.httpx.Client", Client):
        MediaDownloader(tmp_path, max_workers=1).download_all(
            [_post(comments=[comment])]
        )

    assert Client.stream_calls == 1
    assert comment.local_image == "media/comments/BID123_c1.jpg"


@pytest.mark.parametrize("reverse_posts", [False, True])
def test_comment_media_potential_final_path_collision_is_rejected_before_network(
    tmp_path, reverse_posts
):
    extensionless = Post(
        bid="A",
        uid="owner",
        user_name="",
        user_avatar="",
        text="",
        comments=[
            Comment(
                "B_C", "", "", "", "", "", 0,
                image_url="https://wx1.sinaimg.cn/large/opaque-resource",
            )
        ],
    )
    explicit_png = Post(
        bid="A_B",
        uid="owner",
        user_name="",
        user_avatar="",
        text="",
        comments=[
            Comment(
                "C", "", "", "", "", "", 0,
                image_url="https://wx1.sinaimg.cn/large/explicit.png",
            )
        ],
    )
    posts = [extensionless, explicit_png]
    if reverse_posts:
        posts.reverse()

    with patch("weibo_book.media.download_file") as download:
        with pytest.raises(WeiboError, match="潜在最终路径冲突") as exc_info:
            MediaDownloader(tmp_path, max_workers=1).download_all(posts)

    download.assert_not_called()
    assert exc_info.value.kind is WeiboErrorKind.PARSE


def test_comment_media_same_underscore_stem_different_explicit_extensions_allowed(tmp_path):
    jpg_comment = Comment(
        "B_C", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/one.jpg",
    )
    png_comment = Comment(
        "C", "", "", "", "", "", 0,
        image_url="https://wx1.sinaimg.cn/large/two.png",
    )
    posts = [
        Post("A", "owner", "", "", "", comments=[jpg_comment]),
        Post("A_B", "owner", "", "", "", comments=[png_comment]),
    ]

    def write_success(_client, _url, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"image")
        return True

    with patch("weibo_book.media.download_file", side_effect=write_success) as download:
        result = MediaDownloader(tmp_path, max_workers=1).download_all(posts)

    assert download.call_count == 2
    assert result["success"] == 2
    assert jpg_comment.local_image == "media/comments/A_B_C.jpg"
    assert png_comment.local_image == "media/comments/A_B_C.png"


def test_comment_directory_uses_checked_path_fallback_without_directory_descriptors(
    tmp_path, monkeypatch
):
    import weibo_book.media as media_module

    original_open = media_module.os.open
    monkeypatch.setattr(
        media_module, "_SUPPORTS_DIRECTORY_FDS", False, raising=False
    )

    def reject_directory_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is None and Path(path).is_dir():
            raise PermissionError("Windows 不支持以 POSIX 方式打开目录")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(media_module.os, "open", reject_directory_open)

    reference = MediaDownloader(tmp_path, max_workers=1)._open_secure_comment_directory()

    assert isinstance(reference, media_module._CheckedDirectory)
    assert reference.path == tmp_path / "media" / "comments"
    media_module._close_directory_reference(reference)
