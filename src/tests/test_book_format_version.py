"""互动 HTML 版式号：渲染留痕、目录检查识别与书库透传。

版式号规则：V1 = v1.x 旧索引档案（只能重新备份）；V2 = 公开发布的
v2.0.0/v2.0.1 互动 HTML；V3 = v2.1.0 互动 HTML（含搜索与超话页签）；
V4 = v2.2.0 互动 HTML（合并导航与桌面三栏）。当前版式号以 BOOK_FORMAT_VERSION 为准。
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.archive_folder import inspect_archive_folder
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.render_snapshot import BOOK_FORMAT_VERSION
from weibo_book.archive.schema import PostRecord

BOOK_FORMAT_META_KEY = "book_format_version"
FORMAT_META_TAG = f'<meta name="weishushu-book-format" content="{BOOK_FORMAT_VERSION}">'
V3_FINGERPRINT = 'data-view="search"'


def _create_archive(root: Path, uid: str = "10001") -> None:
    repository = ArchiveRepository.create(root, uid=uid, screen_name="测试用户")
    repository.upsert_post(PostRecord(bid="A", uid=uid, text="正文"))
    repository.close()


def _render(root: Path, uid: str = "10001") -> None:
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository = ArchiveRepository.open(root, uid)
    try:
        ArchiveRenderer(repository).render_all(
            root, render_pdf=lambda _html, path: path.write_bytes(b"pdf")
        )
    finally:
        repository.close()


def _read_meta(root: Path, key: str):
    repository = ArchiveRepository.open(root, "10001")
    try:
        return repository._read_meta(key)
    finally:
        repository.close()


def test_render_stamps_format_version_in_html_data_and_database(tmp_path):
    root = tmp_path / "archive"
    _create_archive(root)
    _render(root)

    html = (root / "微博书.html").read_text(encoding="utf-8")
    assert FORMAT_META_TAG in html

    source = (root / "数据与媒体" / "数据" / "archive-data.js").read_text(encoding="utf-8")
    payload = json.loads(source.removeprefix("window.__WEISHUSHU_ARCHIVE__ = ").removesuffix(";\n"))
    assert payload["format_version"] == BOOK_FORMAT_VERSION

    assert _read_meta(root, BOOK_FORMAT_META_KEY) == BOOK_FORMAT_VERSION


def test_inspection_reads_stamped_format_version(tmp_path):
    root = tmp_path / "archive"
    _create_archive(root)
    _render(root)

    inspection = inspect_archive_folder(root, current_uid="10001")
    assert inspection.state == "archive"
    assert inspection.book_format_version == BOOK_FORMAT_VERSION


def test_inspection_fingerprints_unstamped_v3_html(tmp_path):
    root = tmp_path / "archive"
    _create_archive(root)
    (root / "微博书.html").write_text(
        f'<html><head></head><body><section data-view="search" hidden></section></body></html>',
        encoding="utf-8",
    )

    inspection = inspect_archive_folder(root, current_uid="10001")
    assert inspection.state == "archive"
    assert inspection.book_format_version == 3


def test_inspection_fingerprints_v2_html(tmp_path):
    root = tmp_path / "archive"
    _create_archive(root)
    (root / "微博书.html").write_text(
        "<html><head></head><body><div data-view=\"feed\"></div></body></html>",
        encoding="utf-8",
    )

    inspection = inspect_archive_folder(root, current_uid="10001")
    assert inspection.state == "archive"
    assert inspection.book_format_version == 2


def test_inspection_without_html_or_stamp_returns_unknown(tmp_path):
    root = tmp_path / "archive"
    _create_archive(root)

    inspection = inspect_archive_folder(root, current_uid="10001")
    assert inspection.state == "archive"
    assert inspection.book_format_version is None


def test_library_entries_carry_format_version(tmp_path, monkeypatch):
    from backend.app.services import archive_library as library

    monkeypatch.setattr(
        library, "_registry_path", lambda: tmp_path / "state" / "archive-library.json"
    )
    rendered = tmp_path / "rendered"
    _create_archive(rendered)
    _render(rendered)
    old = tmp_path / "old"
    _create_archive(old)
    (old / "微博书.html").write_text(
        "<html><body><div data-view=\"feed\"></div></body></html>", encoding="utf-8"
    )
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".weishushu_index.json").write_text("{}", encoding="utf-8")

    library.register_archive(str(rendered), expected_uid="10001")
    library.register_archive(str(old), expected_uid="10001")
    library._write([*library._read(), {"path": str(legacy), "uid": "10001"}])

    entries = {entry.path: entry for entry in library.list_archives()}
    assert entries[str(rendered)].book_format_version == BOOK_FORMAT_VERSION
    assert entries[str(old)].book_format_version == 2
    assert entries[str(legacy)].available is False
    assert entries[str(legacy)].book_format_version == 1
