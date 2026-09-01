from __future__ import annotations

from pathlib import Path

import pytest

from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import MediaRecord, PostRecord


def _repository(root: Path) -> ArchiveRepository:
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.upsert_post(PostRecord(
        bid="A",
        uid="10001",
        text="正文",
        created_at="2026-07-14T16:38:01+08:00",
    ))
    return repository


def test_cleanup_removes_only_unreferenced_regular_files_in_managed_directories(
    tmp_path,
):
    from weibo_book.archive.media_cleanup import cleanup_unreferenced_media

    require_symlink_capability(target_is_directory=False)
    root = tmp_path / "archive"
    repository = _repository(root)
    kept = root / "media" / "avatars" / "kept.jpg"
    orphan = root / "media" / "avatars" / "orphan.jpg"
    outside = tmp_path / "outside.jpg"
    for path in (kept, orphan, outside):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode("utf-8"))
    link = root / "media" / "avatars" / "outside-link.jpg"
    link.symlink_to(outside)
    repository.upsert_media(MediaRecord(
        "user", "10001", "avatar", 0, "remote", "media/avatars/kept.jpg"
    ))

    removed = cleanup_unreferenced_media(
        root, repository.list_media_for_render()
    )

    assert removed == ["media/avatars/orphan.jpg"]
    assert kept.exists()
    assert link.is_symlink()
    assert outside.read_bytes() == b"outside.jpg"


def test_cleanup_rejects_managed_directory_symlink(tmp_path):
    from weibo_book.archive.media_cleanup import cleanup_unreferenced_media
    from weibo_book.archive.repository import ArchiveError

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.jpg").write_bytes(b"keep")
    media = root / "media"
    media.mkdir()
    (media / "avatars").symlink_to(external, target_is_directory=True)

    with pytest.raises(ArchiveError, match="受管理媒体目录不能是符号链接"):
        cleanup_unreferenced_media(root, [])

    assert (external / "keep.jpg").read_bytes() == b"keep"


def test_renderer_cleans_only_after_fixed_outputs_commit(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    root = tmp_path / "archive"
    repository = _repository(root)
    orphan = root / "media" / "posts" / "orphan.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")

    with pytest.raises(RuntimeError, match="PDF 失败"):
        ArchiveRenderer(repository).render_all(
            root,
            render_pdf=lambda _html, _pdf: (_ for _ in ()).throw(
                RuntimeError("PDF 失败")
            ),
        )
    assert orphan.exists()

    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    assert not orphan.exists()
    assert (root / "微博书.html").is_file()
    assert (root / "微博书.pdf").read_bytes() == b"pdf"


def test_renderer_fsyncs_completed_pdf_with_a_writable_descriptor(tmp_path, monkeypatch):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    target = tmp_path / "微博书.pdf"
    target.write_bytes(b"pdf")
    opened_modes: list[str] = []
    original_open = Path.open

    def track_open(self, mode="r", *args, **kwargs):
        if self == target:
            opened_modes.append(mode)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_open)

    ArchiveRenderer._fsync_file(target)

    assert opened_modes == ["r+b"]


def test_renderer_restores_backup_without_directory_descriptors(tmp_path, monkeypatch):
    import weibo_book.archive.render_snapshot as render_module
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    root = tmp_path / "archive"
    root.mkdir()
    backup = root / "backup.html"
    target = root / "微博书.html"
    backup.write_bytes(b"restored")
    original_open = render_module.os.open
    monkeypatch.setattr(
        render_module, "_SUPPORTS_DIRECTORY_FDS", False, raising=False
    )

    def reject_directory_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is None and Path(path).is_dir():
            raise PermissionError("Windows 不支持以 POSIX 方式打开目录")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(render_module.os, "open", reject_directory_open)

    ArchiveRenderer._restore_backup(backup, target)

    assert target.read_bytes() == b"restored"
