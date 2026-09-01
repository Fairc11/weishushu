from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord
from weibo_book.archive.render_snapshot import ArchiveRenderer


def _post() -> PostRecord:
    return PostRecord(
        bid="A", uid="10001", text="搬移后正文", created_at="2026-07-14T00:00:00+00:00",
        source="", ip_location="", is_pinned=False, pin_order=None,
        visibility="visible", reposts_count=0, comments_count=0, likes_count=0,
    )


def test_publish_crash_then_whole_archive_move_recovers_at_new_location(tmp_path):
    root = tmp_path / "old-parent" / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.upsert_post(_post())
    for index, target in enumerate((
        root / "微博书.html", root / "微博书.pdf", root / "微博书.md",
        root / "data" / "archive-data.js",
    )):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"OLD-{index}".encode())
    repository.close()

    code = r'''
import os
from pathlib import Path
import weibo_book.archive.render_snapshot as module
from weibo_book.archive.repository import ArchiveRepository
root=Path(os.environ["ARCHIVE_ROOT"]); repository=ArchiveRepository.open(root,"10001")
targets={root/"微博书.html",root/"微博书.pdf",root/"微博书.md",root/"data"/"archive-data.js"}; real=module.os.replace
def crash(source,destination,*args,**kwargs):
    result=real(source,destination,*args,**kwargs)
    if Path(destination) in targets and ".render-stage-" in Path(source).as_posix(): os._exit(73)
    return result
module.os.replace=crash
module.ArchiveRenderer(repository).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"CRASH-PDF"))
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ARCHIVE_ROOT": str(root)},
    )
    assert result.returncode == 73
    journal_payload = json.loads(
        (root / "data" / ".weishushu-render-state.json").read_text(encoding="utf-8")
    )
    assert set(journal_payload) == {
        "stage", "had_original", "phase", "published", "restored",
    }
    assert Path(journal_payload["stage"]).name == journal_payload["stage"]

    moved = tmp_path / "new-parent" / "archive"
    moved.parent.mkdir()
    root.rename(moved)
    repository = ArchiveRepository.open(moved, "10001")
    ArchiveRenderer(repository).render_all(
        moved, render_pdf=lambda _html, pdf: pdf.write_bytes(b"FINAL-PDF")
    )

    assert "搬移后正文" in (moved / "data" / "archive-data.js").read_text(encoding="utf-8")
    assert (moved / "微博书.pdf").read_bytes() == b"FINAL-PDF"
    assert not (moved / "data" / ".weishushu-render-state.json").exists()
    assert not list((moved / "data").glob(".render-stage-*"))


def test_relative_archive_root_and_orphan_temporary_cleanup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path("archive")
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.upsert_post(_post())
    orphan = root / "data" / ".render-stage-orphan"
    (orphan / "nested").mkdir(parents=True)
    (orphan / "nested" / "unused").write_bytes(b"unused")
    restore_temps = (
        root / ".微博书.html.restore-dead",
        root / ".微博书.pdf.restore-dead",
        root / ".微博书.md.restore-dead",
        root / "data" / ".archive-data.js.restore-dead",
    )
    for temporary in restore_temps:
        temporary.write_bytes(b"unused")

    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"PDF")
    )

    assert (root / "微博书.html").is_file()
    assert not orphan.exists()
    assert all(not temporary.exists() for temporary in restore_temps)
