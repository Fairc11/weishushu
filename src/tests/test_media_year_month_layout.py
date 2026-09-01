"""阶段 8：媒体文件年-月目录布局与旧档案自动迁移。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.media_layout import (
    MEDIA_LAYOUT_VERSION,
    MEDIA_LAYOUT_VERSION_KEY,
    media_path_shape,
    media_year_month,
    read_media_layout_version,
    write_media_layout_version,
)
from weibo_book.archive.repository import ArchiveError, ArchiveRepository
from weibo_book.archive.schema import (
    CommentRecord,
    MediaRecord,
    PostRecord,
    PostRevisionRecord,
)
from weibo_book.archive.source import ArchiveMediaStager
from weibo_book.errors import WeiboError
from weibo_book.models import Comment, ImageQuality, MediaType, Post, PostMedia


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _post_record(bid: str, created_at: str, **overrides) -> PostRecord:
    values = {
        "bid": bid, "uid": "10001", "text": "正文", "created_at": created_at,
        "source": "", "ip_location": "", "is_pinned": False, "pin_order": None,
        "visibility": "visible", "reposts_count": 0, "comments_count": 0,
        "likes_count": 0,
    }
    values.update(overrides)
    return PostRecord(**values)


def _write_media(root: Path, relative: str, payload: bytes) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _make_legacy_archive(root: Path) -> ArchiveRepository:
    """创建档案后移除布局版本键，模拟旧版平铺布局档案。"""
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository._connection.execute(
        "DELETE FROM archive_meta WHERE key = ?", (MEDIA_LAYOUT_VERSION_KEY,)
    )
    return repository


class _FakeDownloader:
    """按旧版平铺方式把媒体写进工作目录的下载器替身。"""

    def __init__(self, root, image_quality):
        self.root = Path(root)
        self._cancel_event = None

    def download_all(self, posts):
        def place(value):
            for media in value.media:
                target = self.root / "media" / f"{value.bid}_raw_{media.url.rsplit('/', 1)[-1]}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"bytes:{value.bid}:{media.url}".encode())
                media.local_path = str(target)
            if value.retweeted is not None:
                place(value.retweeted)

        for post in posts:
            post.comments = getattr(post, "comments", [])
            place(post)
            for comment in post.comments:
                if comment.image_url:
                    target = self.root / "media" / "comments" / f"{post.bid}_{comment.id}.jpg"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f"comment:{comment.id}".encode())
                    comment.local_image = f"media/comments/{post.bid}_{comment.id}.jpg"
        return {"total": 0, "success": 0, "fail": 0, "failed": []}

    def download_avatar(self, url, identity):
        target = self.root / "media" / "avatars" / f"{identity.replace(':', '-')}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(url.encode())
        return target


def _stage(post: Post, comments: list[Comment], work_root: Path):
    stager = ArchiveMediaStager(
        image_quality=ImageQuality.ORIGINAL, downloader_factory=_FakeDownloader
    )
    return stager.stage(post, comments, work_root)


# ---------------------------------------------------------------- 路径形状


def test_media_year_month_parses_datetime_and_strings():
    assert media_year_month(datetime(2026, 7, 14, 16, 38)) == ("2026", "07")
    assert media_year_month("2026-07-14T16:38:01+08:00") == ("2026", "07")
    assert media_year_month("2020-01-02 03:04") == ("2020", "01")
    assert media_year_month("") is None
    assert media_year_month(None) is None
    assert media_year_month("不是时间") is None
    assert media_year_month("2026-13-01T00:00:00") is None


def test_media_path_shape_accepts_flat_and_year_month_layouts():
    assert media_path_shape("media/posts/x.jpg") == ("media", "posts", "x.jpg")
    assert media_path_shape("media/comments/x.jpg") == ("media", "comments", "x.jpg")
    assert media_path_shape("media/avatars/x.png") == ("media", "avatars", "x.png")
    # 既有校验允许的两段平铺路径保持不变
    assert media_path_shape("media/x.jpg") == ("media", "x.jpg")
    assert media_path_shape("media/posts/2026/07/x.jpg") == (
        "media", "posts", "2026", "07", "x.jpg",
    )
    assert media_path_shape("media/comments/2020/12/x.webp") == (
        "media", "comments", "2020", "12", "x.webp",
    )


@pytest.mark.parametrize("value", [
    "",
    "media",
    "media/posts/202A/07/x.jpg",
    "media/posts/2026/7/x.jpg",
    "media/posts/2026/13/x.jpg",
    "media/posts/2026/00/x.jpg",
    "media/posts/2026/x.jpg",
    "media/posts/2026/07/extra/x.jpg",
    "media/posts/../07/x.jpg",
    "media/posts//x.jpg",
    "media\\posts\\x.jpg",
    "/media/posts/x.jpg",
    "posts/2026/07/x.jpg",
])
def test_media_path_shape_rejects_invalid(value):
    assert media_path_shape(value) is None


# ---------------------------------------------------------------- 暂存新布局


def test_stager_places_post_media_under_owner_year_month(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        created_at=datetime(2026, 7, 14, 16, 38),
        media=[PostMedia(MediaType.IMAGE, "https://example.test/A.jpg")],
    )
    staged = _stage(post, [], tmp_path / "work")

    paths = {item.record.local_path for item in staged}
    assert any(path.startswith("media/posts/2026/07/A_image_0_") for path in paths)
    assert post.media[0].local_path.startswith("media/posts/2026/07/A_image_0_")
    assert all(item.staged_path.is_file() for item in staged)


def test_stager_places_retweeted_media_under_original_post_month(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        created_at=datetime(2026, 7, 14, 16, 38),
        media=[PostMedia(MediaType.IMAGE, "https://example.test/A.jpg")],
        retweeted=Post(
            bid="R", uid="20002", user_name="原作者", user_avatar="", text="r",
            created_at=datetime(2020, 1, 2, 3, 4),
            media=[PostMedia(MediaType.IMAGE, "https://example.test/R.jpg")],
        ),
    )
    staged = _stage(post, [], tmp_path / "work")

    paths = {item.record.local_path for item in staged}
    assert any(path.startswith("media/posts/2026/07/A_image_0_") for path in paths)
    assert any(path.startswith("media/posts/2020/01/R_image_0_") for path in paths)


def test_stager_relocates_comment_images_to_post_month(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        created_at=datetime(2026, 7, 14, 16, 38),
    )
    comment = Comment(
        "c1", "评论", "用户", "2", "", "", 0,
        image_url="https://example.test/c1.jpg",
    )
    work_root = tmp_path / "work"

    staged = _stage(post, [comment], work_root)

    assert comment.local_image == "media/comments/2026/07/A_c1.jpg"
    assert (work_root / "media" / "comments" / "2026" / "07" / "A_c1.jpg").is_file()
    assert not (work_root / "media" / "comments" / "A_c1.jpg").exists()
    assert {
        (item.record.owner_type, item.record.role)
        for item in staged
    } == {("comment", "image")}

    # 同帖重复同步：sha256 相同直接复用，不报错也不产生第二份文件
    post2 = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        created_at=datetime(2026, 7, 14, 16, 38),
    )
    comment2 = Comment(
        "c1", "评论", "用户", "2", "", "", 0,
        image_url="https://example.test/c1.jpg",
    )
    staged2 = _stage(post2, [comment2], work_root)
    assert comment2.local_image == "media/comments/2026/07/A_c1.jpg"
    assert len(list((work_root / "media" / "comments" / "2026" / "07").iterdir())) == 1
    assert staged2[0].record.sha256 == staged[0].record.sha256


def test_stager_keeps_avatars_flat(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人",
        user_avatar="https://img.test/main.png", text="x",
        created_at=datetime(2026, 7, 14, 16, 38),
    )
    staged = _stage(post, [], tmp_path / "work")

    assert [item.record.local_path for item in staged] == ["media/avatars/user-10001.png"]


def test_stager_rejects_media_without_created_at(tmp_path):
    post = Post(
        bid="A", uid="10001", user_name="本人", user_avatar="", text="x",
        media=[PostMedia(MediaType.IMAGE, "https://example.test/A.jpg")],
    )
    with pytest.raises(WeiboError, match="发布时间"):
        _stage(post, [], tmp_path / "work")


# ---------------------------------------------------------------- 写入与渲染校验


def test_upsert_media_accepts_year_month_and_rejects_bad_shape(tmp_path):
    repository = ArchiveRepository.create(tmp_path / "archive", "10001", "本人")
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/2026/07/x.jpg", "0" * 64,
    ))
    with pytest.raises(ArchiveError):
        repository.upsert_media(MediaRecord(
            "post", "A", "image", 1, "remote", "media/posts/2026/13/x.jpg", "0" * 64,
        ))
    with pytest.raises(ArchiveError):
        repository.upsert_media(MediaRecord(
            "post", "A", "image", 2, "remote", "media/posts/2026/07/deep/x.jpg", "0" * 64,
        ))


def test_safe_local_path_accepts_year_month_and_rejects_bad_shape():
    from weibo_book.archive.render_snapshot import _safe_local_path

    assert _safe_local_path("media/posts/2026/07/x.jpg") == "media/posts/2026/07/x.jpg"
    assert _safe_local_path("media/comments/2020/12/x.webp") == "media/comments/2020/12/x.webp"
    for bad in (
        "media/posts/2026/13/x.jpg",
        "media/posts/2026/07/extra/x.jpg",
        "media/posts/2026/x.jpg",
    ):
        with pytest.raises(ArchiveError, match="归档媒体路径不安全"):
            _safe_local_path(bad)


# ---------------------------------------------------------------- 清理递归


def test_cleanup_recurses_year_month_directories(tmp_path):
    from weibo_book.archive.media_cleanup import cleanup_unreferenced_media

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    kept = "media/posts/2026/07/kept.jpg"
    repository.upsert_media(MediaRecord("post", "A", "image", 0, "remote", kept, "0" * 64))
    _write_media(root, kept, b"kept")
    _write_media(root, "media/posts/2026/07/orphan.jpg", b"orphan")
    _write_media(root, "media/comments/2020/01/orphan.png", b"orphan")
    _write_media(root, "media/avatars/orphan.png", b"orphan")

    removed = cleanup_unreferenced_media(root, repository.list_media_for_render())

    assert removed == [
        "media/avatars/orphan.png",
        "media/comments/2020/01/orphan.png",
        "media/posts/2026/07/orphan.jpg",
    ]
    assert root.joinpath(*kept.split("/")).is_file()


def test_cleanup_rejects_symlinked_month_directory(tmp_path):
    from weibo_book.archive.media_cleanup import cleanup_unreferenced_media

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.jpg").write_bytes(b"keep")
    month_dir = root / "media" / "posts" / "2026"
    month_dir.mkdir(parents=True)
    (month_dir / "07").symlink_to(external, target_is_directory=True)

    with pytest.raises(ArchiveError, match="受管理媒体目录不能是符号链接"):
        cleanup_unreferenced_media(root, [])

    assert (external / "keep.jpg").read_bytes() == b"keep"


# ---------------------------------------------------------------- 迁移


def test_new_archive_has_current_media_layout_version(tmp_path):
    repository = ArchiveRepository.create(tmp_path / "archive", "10001", "本人")
    assert read_media_layout_version(repository._connection) == MEDIA_LAYOUT_VERSION


def test_open_migrates_flat_media_to_year_month(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    repository.upsert_post(_post_record(
        "B", "2026-08-01T00:00:00+08:00",
        retweeted_payload={
            "bid": "R", "uid": "20002", "created_at": "2020-01-02T03:04:05+08:00",
        },
    ))
    repository.replace_current_comments("A", [
        CommentRecord("c1", "A", None, {"text": "评论"}, "2026-07-14T17:00:00+08:00"),
    ])
    post_payload = b"post-image"
    retweeted_payload = b"retweeted-image"
    comment_payload = b"comment-image"
    avatar_payload = b"avatar"
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/A_image_0_aa.jpg",
        _sha256(post_payload),
    ))
    repository.upsert_media(MediaRecord(
        "post", "R", "image", 0, "remote", "media/posts/R_image_0_bb.jpg",
        _sha256(retweeted_payload),
    ))
    repository.upsert_media(MediaRecord(
        "comment", "c1", "image", 0, "remote", "media/comments/A_c1.jpg",
        _sha256(comment_payload),
    ))
    repository.upsert_media(MediaRecord(
        "user", "10001", "avatar", 0, "remote", "media/avatars/user-10001.png",
        _sha256(avatar_payload),
    ))
    _write_media(root, "media/posts/A_image_0_aa.jpg", post_payload)
    _write_media(root, "media/posts/R_image_0_bb.jpg", retweeted_payload)
    _write_media(root, "media/comments/A_c1.jpg", comment_payload)
    _write_media(root, "media/avatars/user-10001.png", avatar_payload)
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    paths = {item.local_path for item in reopened.list_media_for_render()}
    assert paths == {
        "media/posts/2026/07/A_image_0_aa.jpg",
        "media/posts/2020/01/R_image_0_bb.jpg",
        "media/comments/2026/07/A_c1.jpg",
        "media/avatars/user-10001.png",
    }
    assert _sha256((root / "media/posts/2026/07/A_image_0_aa.jpg").read_bytes()) == _sha256(post_payload)
    assert _sha256((root / "media/posts/2020/01/R_image_0_bb.jpg").read_bytes()) == _sha256(retweeted_payload)
    assert _sha256((root / "media/comments/2026/07/A_c1.jpg").read_bytes()) == _sha256(comment_payload)
    assert not (root / "media/posts/A_image_0_aa.jpg").exists()
    assert not (root / "media/comments/A_c1.jpg").exists()
    assert _sha256((root / "media/avatars/user-10001.png").read_bytes()) == _sha256(avatar_payload)
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()


def test_migration_tolerates_missing_source_files(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/A_image_0_aa.jpg", "0" * 64,
    ))
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    assert [item.local_path for item in reopened.list_media_for_render()] == [
        "media/posts/2026/07/A_image_0_aa.jpg"
    ]
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()


def test_migration_resumes_from_partial_state(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    repository.upsert_post(_post_record("B", "2025-03-09T10:00:00+08:00"))
    payload_a = b"image-a"
    payload_b = b"image-b"
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/A_image_0_aa.jpg",
        _sha256(payload_a),
    ))
    repository.upsert_media(MediaRecord(
        "post", "B", "image", 0, "remote", "media/posts/B_image_0_bb.jpg",
        _sha256(payload_b),
    ))
    _write_media(root, "media/posts/A_image_0_aa.jpg", payload_a)
    _write_media(root, "media/posts/B_image_0_bb.jpg", payload_b)
    # 模拟上次迁移中断：A 已移动并更新，B 仍是旧布局
    migrated_dir = root / "media" / "posts" / "2026" / "07"
    migrated_dir.mkdir(parents=True)
    (root / "media" / "posts" / "A_image_0_aa.jpg").rename(migrated_dir / "A_image_0_aa.jpg")
    repository._connection.execute(
        "UPDATE media SET local_path = ? WHERE owner_id = 'A'",
        ("media/posts/2026/07/A_image_0_aa.jpg",),
    )
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    paths = {item.local_path for item in reopened.list_media_for_render()}
    assert paths == {
        "media/posts/2026/07/A_image_0_aa.jpg",
        "media/posts/2025/03/B_image_0_bb.jpg",
    }
    assert (migrated_dir / "A_image_0_aa.jpg").read_bytes() == payload_a
    assert (root / "media" / "posts" / "2025" / "03" / "B_image_0_bb.jpg").read_bytes() == payload_b
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()


def test_migration_failure_keeps_old_layout_usable(tmp_path, caplog):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/A_image_0_aa.jpg", "f" * 64,
    ))
    _write_media(root, "media/posts/A_image_0_aa.jpg", b"tampered")
    repository.close()

    with caplog.at_level("WARNING"):
        reopened = ArchiveRepository.open(root, "10001")

    # 校验和不匹配：迁移中止，旧布局保持可用，版本不前进
    assert read_media_layout_version(reopened._connection) == 1
    assert [item.local_path for item in reopened.list_media_for_render()] == [
        "media/posts/A_image_0_aa.jpg"
    ]
    assert (root / "media" / "posts" / "A_image_0_aa.jpg").read_bytes() == b"tampered"
    assert any("迁移失败" in record.getMessage() for record in caplog.records)
    reopened.close()


def test_render_snapshot_works_after_migration(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    payload = b"post-image"
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/A_image_0_aa.jpg",
        _sha256(payload),
    ))
    _write_media(root, "media/posts/A_image_0_aa.jpg", payload)
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")
    snapshot = ArchiveRenderSnapshot.from_repository(reopened)

    assert snapshot.posts[0]["media"][0]["local_path"] == "media/posts/2026/07/A_image_0_aa.jpg"
    reopened.close()


# ---------------------------------------------------------------- 内嵌 JSON 改写（v3）


def _legacy_embedded_post(bid: str, created_at: str) -> PostRecord:
    """模拟平铺布局时代写入的内嵌 JSON：媒体路径没有年-月目录。"""
    return _post_record(
        bid, created_at,
        retweeted_payload={
            "bid": "R", "uid": "20002", "created_at": "2020-01-02T03:04:05+08:00",
            "media": [{"type": "image", "position": 0,
                        "local_path": "media/posts/R_image_0_bb.jpg"}],
            "link_card": {"local_image": "media/posts/R_link_card_0_cc.jpg"},
        },
        link_card_payload={"local_image": f"media/posts/{bid}_link_card_0_dd.jpg"},
        media_signature=[{"type": "image", "position": 0,
                          "local_path": f"media/posts/{bid}_image_0_aa.jpg"}],
    )


def test_migration_rewrites_embedded_json_paths(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.upsert_post(_legacy_embedded_post("A", "2026-07-14T16:38:01+08:00"))
    repository.add_post_revision(PostRevisionRecord(
        bid="A", revision_no=1, captured_at="2026-07-15T00:00:00+08:00",
        payload={"media_signature": [
            {"type": "image", "position": 0,
             "local_path": "media/posts/A_image_0_aa.jpg"},
        ]},
        content_hash="0" * 64,
    ))
    repository.replace_current_comments("A", [
        CommentRecord("c1", "A", None,
                      {"text": "评论", "image": "media/comments/A_c1.jpg"},
                      "2026-07-14T17:00:00+08:00"),
    ])
    for owner, role, flat in (
        ("A", "image", "media/posts/A_image_0_aa.jpg"),
        ("A", "link_card", "media/posts/A_link_card_0_dd.jpg"),
        ("R", "image", "media/posts/R_image_0_bb.jpg"),
        ("R", "link_card", "media/posts/R_link_card_0_cc.jpg"),
        ("c1", "image", "media/comments/A_c1.jpg"),
    ):
        owner_type = "comment" if owner == "c1" else "post"
        payload = f"bytes:{flat}".encode()
        repository.upsert_media(MediaRecord(
            owner_type, owner, role, 0, "remote", flat, _sha256(payload),
        ))
        _write_media(root, flat, payload)
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    row = reopened._connection.execute(
        "SELECT retweeted_json, link_card_json, media_signature_json "
        "FROM posts WHERE bid = 'A'"
    ).fetchone()
    retweeted = json.loads(row[0])
    assert retweeted["media"][0]["local_path"] == (
        "media/posts/2020/01/R_image_0_bb.jpg"
    )
    assert retweeted["link_card"]["local_image"] == (
        "media/posts/2020/01/R_link_card_0_cc.jpg"
    )
    assert json.loads(row[1])["local_image"] == (
        "media/posts/2026/07/A_link_card_0_dd.jpg"
    )
    assert json.loads(row[2])[0]["local_path"] == (
        "media/posts/2026/07/A_image_0_aa.jpg"
    )
    revision_payload = reopened._connection.execute(
        "SELECT payload_json FROM post_revisions WHERE bid = 'A'"
    ).fetchone()[0]
    assert json.loads(revision_payload)["media_signature"][0]["local_path"] == (
        "media/posts/2026/07/A_image_0_aa.jpg"
    )
    comment_payload = reopened._connection.execute(
        "SELECT payload_json FROM comments WHERE id = 'c1'"
    ).fetchone()[0]
    assert json.loads(comment_payload)["image"] == (
        "media/comments/2026/07/A_c1.jpg"
    )
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()


def test_v2_archive_rewrites_embedded_json_without_moving_files(tmp_path):
    """已是年-月布局（v2）的档案：文件不动，只改写内嵌 JSON 的旧路径。"""
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    write_media_layout_version(repository._connection, 2)
    repository.upsert_post(_post_record(
        "A", "2026-07-14T16:38:01+08:00",
        retweeted_payload={
            "bid": "R", "uid": "20002", "created_at": "2020-01-02T03:04:05+08:00",
            "media": [{"type": "image", "position": 0,
                        "local_path": "media/posts/R_image_0_bb.jpg"}],
        },
    ))
    migrated = "media/posts/2020/01/R_image_0_bb.jpg"
    payload = b"retweeted-image"
    repository.upsert_media(MediaRecord(
        "post", "R", "image", 0, "remote", migrated, _sha256(payload),
    ))
    _write_media(root, migrated, payload)
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    retweeted = json.loads(reopened._connection.execute(
        "SELECT retweeted_json FROM posts WHERE bid = 'A'"
    ).fetchone()[0])
    assert retweeted["media"][0]["local_path"] == migrated
    assert (root / "media/posts/2020/01/R_image_0_bb.jpg").read_bytes() == payload
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()


def test_embedded_rewrite_keeps_unresolvable_and_conflicting_refs(tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    write_media_layout_version(repository._connection, 2)
    repository.upsert_post(_post_record(
        "A", "2026-07-14T16:38:01+08:00",
        link_card_payload={"local_image": "media/posts/Gone_link_card_0_zz.jpg"},
        media_signature=[{"type": "image", "position": 0,
                          "local_path": "media/posts/dup.jpg"}],
    ))
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/2026/07/dup.jpg", "0" * 64,
    ))
    repository.upsert_media(MediaRecord(
        "post", "B", "image", 0, "remote", "media/posts/2025/03/dup.jpg", "1" * 64,
    ))
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    row = reopened._connection.execute(
        "SELECT link_card_json, media_signature_json FROM posts WHERE bid = 'A'"
    ).fetchone()
    # media 表里没有的文件名保持原样（本来就没有文件，渲染侧有容错）
    assert json.loads(row[0])["local_image"] == "media/posts/Gone_link_card_0_zz.jpg"
    # 文件名冲突（多个年-月路径同名）时不猜测，保留原路径
    assert json.loads(row[1])[0]["local_path"] == "media/posts/dup.jpg"
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    reopened.close()
