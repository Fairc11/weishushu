"""根布局 v1→v2 迁移、双布局识别与中文媒体路径形状。"""

from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from weibo_book.archive.media_layout import (
    MEDIA_LAYOUT_VERSION,
    MEDIA_LAYOUT_VERSION_KEY,
    media_path_shape,
    read_media_layout_version,
    write_media_layout_version,
)
from weibo_book.archive.paths import (
    ASSETS_DIR_NAME,
    DATA_DIR_NAME,
    MEDIA_ROOT_NAME,
    ROOT_LAYOUT_VERSION,
    ROOT_LAYOUT_VERSION_KEY,
    ArchivePaths,
    detect_root_layout,
    migrate_root_layout,
    normalize_nfc,
    read_root_layout_version,
)
from weibo_book.archive.repository import ArchiveError, ArchiveRepository
from weibo_book.archive.schema import MediaRecord, PostRecord
from weibo_book.archive.sync import PersonalArchiveSync
from weibo_book.errors import WeiboError
from backend.app.services.archive_folder import inspect_archive_folder


def _post_record(bid: str, created_at: str, **overrides) -> PostRecord:
    values = {
        "bid": bid, "uid": "10001", "text": "正文", "created_at": created_at,
        "source": "", "ip_location": "", "is_pinned": False, "pin_order": None,
        "visibility": "visible", "reposts_count": 0, "comments_count": 0,
        "likes_count": 0,
    }
    values.update(overrides)
    return PostRecord(**values)


def _make_legacy_archive(root: Path) -> ArchiveRepository:
    """创建档案后回退到根布局 v1：清单回根目录、数据回 ``data/``、媒体回 ``media/``。"""
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()
    assets = root / ASSETS_DIR_NAME
    data = assets / DATA_DIR_NAME
    legacy_data = root / "data"
    legacy_data.mkdir()
    for entry in sorted(data.iterdir(), key=lambda item: item.name):
        if entry.name == "manifest.json":
            continue
        os.replace(entry, legacy_data / entry.name)
    os.replace(data / "manifest.json", root / "manifest.json")
    media = assets / MEDIA_ROOT_NAME
    if media.exists():
        os.replace(media, root / "media")
    data.rmdir()
    assets.rmdir()
    reopened = ArchiveRepository.open(root, "10001")
    reopened._connection.execute(
        "DELETE FROM archive_meta WHERE key IN (?, ?)",
        (MEDIA_LAYOUT_VERSION_KEY, ROOT_LAYOUT_VERSION_KEY),
    )
    return reopened


def _write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


# ---------------------------------------------------------------- 创建与识别


def test_create_produces_root_layout_v2(tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")

    assert repository.paths.version == ROOT_LAYOUT_VERSION
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "manifest.json").is_file()
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "archive.db").is_file()
    assert not (root / "manifest.json").exists()
    assert not (root / "data").exists()
    assert detect_root_layout(root) == ROOT_LAYOUT_VERSION
    assert read_root_layout_version(repository._connection) == ROOT_LAYOUT_VERSION
    assert read_media_layout_version(repository._connection) == MEDIA_LAYOUT_VERSION
    repository.close()


def test_detect_root_layout_defaults_to_v1_and_prefers_v2_manifest(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_root_layout(empty) == 1

    legacy = tmp_path / "legacy"
    _write_file(legacy / "manifest.json", b"{}")
    assert detect_root_layout(legacy) == 1

    modern = tmp_path / "modern"
    _write_file(modern / ASSETS_DIR_NAME / DATA_DIR_NAME / "manifest.json", b"{}")
    assert detect_root_layout(modern) == ROOT_LAYOUT_VERSION

    # NFD 存储名（macOS APFS 可能出现）同样识别为 v2
    nfd = tmp_path / "nfd"
    nfd_assets = nfd / unicodedata.normalize("NFD", ASSETS_DIR_NAME)
    _write_file(
        nfd_assets / unicodedata.normalize("NFD", DATA_DIR_NAME) / "manifest.json",
        b"{}",
    )
    assert detect_root_layout(nfd) == ROOT_LAYOUT_VERSION


def test_archive_paths_rejects_unknown_version(tmp_path):
    with pytest.raises(WeiboError):
        ArchivePaths(tmp_path, 3)


# ---------------------------------------------------------------- 路径形状


def test_media_path_shape_accepts_chinese_subdirectories_with_nfc_compare():
    assert media_path_shape("媒体/微博图片/2026/08/x.jpg") == (
        "媒体", "微博图片", "2026", "08", "x.jpg"
    )
    assert media_path_shape("媒体/评论图片/x.jpg") == ("媒体", "评论图片", "x.jpg")
    assert media_path_shape("媒体/头像/x.png") == ("媒体", "头像", "x.png")
    assert media_path_shape("媒体/封面/x.png") == ("媒体", "封面", "x.png")
    nfd_value = unicodedata.normalize("NFD", "媒体/微博图片/x.jpg")
    assert media_path_shape(nfd_value) == ("媒体", "微博图片", "x.jpg")


def test_media_path_shape_rejects_unknown_chinese_subdirectory():
    assert media_path_shape("媒体/未知子目录/x.jpg") is None
    assert media_path_shape("媒体/x.jpg") is None
    assert media_path_shape("媒体/微博图片/2026/08/深一层/x.jpg") is None
    # media/ 前缀保持旧语义：允许平铺与任意子目录（迁移输入用）
    assert media_path_shape("media/x.jpg") == ("media", "x.jpg")
    assert media_path_shape("media/posts/2026/08/x.jpg") is not None


# ---------------------------------------------------------------- 完整迁移


def test_full_migration_moves_files_and_rewrites_media_prefixes(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    # 已是媒体布局 v3（年-月目录、media/ 前缀）的旧档案
    write_media_layout_version(repository._connection, 3)
    repository.upsert_post(_post_record(
        "A", "2026-07-14T16:38:01+08:00",
        media_signature=[{"type": "image", "position": 0,
                          "local_path": "media/posts/2026/07/a.jpg"}],
    ))
    payload = b"image-bytes"
    repository.upsert_media(MediaRecord(
        "post", "A", "image", 0, "remote", "media/posts/2026/07/a.jpg", "0" * 64,
    ))
    _write_file(root / "media" / "posts" / "2026" / "07" / "a.jpg", payload)
    repository.close()
    assert detect_root_layout(root) == 1

    assert migrate_root_layout(root) is True
    assert detect_root_layout(root) == ROOT_LAYOUT_VERSION
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "manifest.json").is_file()
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "archive.db").is_file()
    assert (root / ASSETS_DIR_NAME / MEDIA_ROOT_NAME / "posts" / "2026" / "07" / "a.jpg").is_file()
    assert not (root / "manifest.json").exists()
    assert not (root / "data").exists()
    assert not (root / "media").exists()

    reopened = ArchiveRepository.open(root, "10001")
    assert reopened.media_layout_migrated is True
    assert read_media_layout_version(reopened._connection) == MEDIA_LAYOUT_VERSION
    assert read_root_layout_version(reopened._connection) == ROOT_LAYOUT_VERSION
    media = reopened.list_media_for_render()
    assert [item.local_path for item in media] == ["媒体/微博图片/2026/07/a.jpg"]
    embedded = json.loads(reopened._connection.execute(
        "SELECT media_signature_json FROM posts WHERE bid = 'A'"
    ).fetchone()[0])
    assert embedded[0]["local_path"] == "媒体/微博图片/2026/07/a.jpg"
    assert (
        root / ASSETS_DIR_NAME / MEDIA_ROOT_NAME / "微博图片" / "2026" / "07" / "a.jpg"
    ).read_bytes() == payload
    assert reopened.manifest().uid == "10001"
    reopened.close()


def test_migrate_root_layout_resumes_from_interrupted_state(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.close()
    # 模拟断点：数据目录已搬走，清单与媒体仍在旧位置
    assets = root / ASSETS_DIR_NAME
    assets.mkdir()
    os.replace(root / "data", assets / DATA_DIR_NAME)
    assert migrate_root_layout(root) is True
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "manifest.json").is_file()
    assert not (root / "manifest.json").exists()
    # 幂等：再次执行无事发生
    assert migrate_root_layout(root) is False
    reopened = ArchiveRepository.open(root, "10001")
    assert reopened.manifest().uid == "10001"
    reopened.close()


def test_migrate_root_layout_rejects_target_conflict(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    _write_file(root / "data" / "extra.txt", b"legacy")
    _write_file(root / ASSETS_DIR_NAME / DATA_DIR_NAME / "extra.txt", b"conflict")
    repository.close()

    with pytest.raises(WeiboError, match="冲突"):
        migrate_root_layout(root)


def test_migrate_root_layout_canonicalizes_nfd_assets_directory(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    _write_file(root / "data" / "extra.txt", b"legacy")
    repository.close()
    # 已存在 NFD 命名的 数据与媒体 目录（macOS 可能落盘为 NFD）
    nfd_assets = root / unicodedata.normalize("NFD", ASSETS_DIR_NAME)
    nfd_assets.mkdir()

    assert migrate_root_layout(root) is True
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "manifest.json").is_file()
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "extra.txt").read_bytes() == b"legacy"
    assert not nfd_assets.exists() or nfd_assets == root / ASSETS_DIR_NAME


def test_open_v1_archive_does_not_migrate_root_layout(tmp_path):
    root = tmp_path / "archive"
    repository = _make_legacy_archive(root)
    repository.close()

    reopened = ArchiveRepository.open(root, "10001")

    assert reopened.paths.version == 1
    assert detect_root_layout(root) == 1
    assert (root / "manifest.json").is_file()
    assert not (root / ASSETS_DIR_NAME).exists()
    assert read_media_layout_version(reopened._connection) == 3  # 根布局 v1 封顶
    reopened.close()


# ---------------------------------------------------------------- 只读识别


def test_inspect_archive_folder_recognizes_both_layouts_without_writes(tmp_path):
    modern = tmp_path / "modern"
    repository = ArchiveRepository.create(modern, "10001", "本人")
    repository.close()
    assert inspect_archive_folder(modern, "10001").state == "archive"
    assert detect_root_layout(modern) == ROOT_LAYOUT_VERSION

    legacy = tmp_path / "legacy"
    legacy_repository = _make_legacy_archive(legacy)
    legacy_repository.close()
    inspection = inspect_archive_folder(legacy, "10001")
    assert inspection.state == "archive"
    # 只读识别不触发迁移
    assert (legacy / "manifest.json").is_file()
    assert not (legacy / ASSETS_DIR_NAME).exists()
    assert detect_root_layout(legacy) == 1


# ---------------------------------------------------------------- 迁移后重渲染


def test_render_after_layout_migration_refreshes_outputs_without_pdf(tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    sync = PersonalArchiveSync(root, source=None, identity_provider=None)
    sync._layout_migrated_fs = True

    sync._render_after_layout_migration(repository)

    assert (root / "微博书.html").is_file()
    assert (root / "微博书.md").is_file()
    assert (root / ASSETS_DIR_NAME / DATA_DIR_NAME / "archive-data.js").is_file()
    assert not (root / "微博书.pdf").exists()
    assert sync._layout_migrated_fs is False
    repository.close()


def test_render_after_layout_migration_noop_without_migration(tmp_path):
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.upsert_post(_post_record("A", "2026-07-14T16:38:01+08:00"))
    sync = PersonalArchiveSync(root, source=None, identity_provider=None)

    sync._render_after_layout_migration(repository)

    assert not (root / "微博书.html").exists()
    repository.close()
