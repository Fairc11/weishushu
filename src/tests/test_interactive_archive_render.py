from __future__ import annotations

import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import shutil
import subprocess
import sys

import pytest
from jinja2 import DictLoader

from tests.symlink_capability import require_symlink_capability
from weibo_book.archive.repository import ArchiveError, ArchiveRepository
from weibo_book.archive.schema import CommentRecord, MediaRecord, PostRecord


_LOCAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082"
)


def _write_local_png(root: Path, relative_path: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_LOCAL_PNG)


def _write_large_local_png(
    root: Path, relative_path: str, color: tuple[int, int, int]
) -> None:
    from PIL import Image

    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1200, 1600), color).save(path, "PNG")


def _add_live_photo_pair(
    repo: ArchiveRepository,
    root: Path,
    bid: str,
    position: int,
    image_path: str,
    video_path: str,
) -> None:
    _write_local_png(root, image_path)
    video = root / video_path
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"local-live-photo-video")
    repo.upsert_media(
        MediaRecord(
            "post", bid, "live_photo_thumbnail", position, "remote-image", image_path
        )
    )
    repo.upsert_media(
        MediaRecord(
            "post", bid, "live_photo", position, "remote-video", video_path
        )
    )


def _install_valid_webm(video) -> None:
    video.evaluate(
        """async video => {
            const canvas = document.createElement('canvas');
            canvas.width = 16;
            canvas.height = 16;
            const stream = canvas.captureStream(10);
            const recorder = new MediaRecorder(stream, {mimeType: 'video/webm'});
            const chunks = [];
            recorder.ondataavailable = event => chunks.push(event.data);
            const stopped = new Promise(resolve => recorder.onstop = resolve);
            recorder.start();
            canvas.getContext('2d').fillRect(0, 0, 16, 16);
            await new Promise(resolve => setTimeout(resolve, 120));
            recorder.stop();
            await stopped;
            video.src = URL.createObjectURL(
                new Blob(chunks, {type: 'video/webm'})
            );
            await new Promise((resolve, reject) => {
                video.onloadedmetadata = resolve;
                video.onerror = reject;
                video.load();
            });
        }"""
    )


def _repo(tmp_path: Path) -> tuple[ArchiveRepository, Path]:
    root = tmp_path / "archive"
    return ArchiveRepository.create(root, "10001", "固定名字"), root


def _post(bid: str, created_at: str, **changes) -> PostRecord:
    values = {"bid": bid, "uid": "10001", "text": f"正文 {bid}", "created_at": created_at}
    values.update(changes)
    return PostRecord(**values)


def test_repository_render_reads_are_exact_and_do_not_change_database(tmp_path):
    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    repo.replace_current_comments("A", [CommentRecord("C", "A", None, {"text": "评论"}, "now")])
    repo.upsert_media(MediaRecord("post", "A", "image", 0, "https://remote.invalid/a", "media/a.jpg"))
    before = (root / "data" / "archive.db").read_bytes()

    assert [post.bid for post in repo.list_posts_for_render()] == ["A"]
    assert [comment.id for comment in repo.list_comments_for_render()] == ["C"]
    assert [media.local_path for media in repo.list_media_for_render()] == ["media/a.jpg"]
    assert (root / "data" / "archive.db").read_bytes() == before


def test_snapshot_sorts_pins_then_newest_and_keeps_unavailable_text(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    for post in (
        _post("OLD", "2025-01-01T00:00:00+00:00", visibility="unavailable"),
        _post("BAD", "不是时间"),
        _post("NEW", "2026-07-14T02:00:00+00:00"),
        _post("PIN-NONE", "", is_pinned=True, pin_order=None),
        _post("PIN-2", "", is_pinned=True, pin_order=2),
        _post("PIN-1", "", is_pinned=True, pin_order=1),
    ):
        repo.upsert_post(post)

    snapshot = ArchiveRenderSnapshot.from_repository(repo)

    assert [post["bid"] for post in snapshot.posts] == [
        "PIN-1", "PIN-2", "PIN-NONE", "NEW", "OLD", "BAD"
    ]
    old = next(post for post in snapshot.posts if post["bid"] == "OLD")
    assert old["text"] == "正文 OLD"
    assert old["visibility"] == "unavailable"
    with pytest.raises(TypeError, match="只读"):
        old["text"] = "不得修改"


def test_snapshot_projects_readable_times_and_normalized_external_text(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post(
        "A",
        "2026-07-14T16:38:01+08:00",
        text="正文 &quot;内容&quot;",
        retweeted_payload={
            "bid": "R",
            "uid": "200",
            "text": "转发 &quot;内容&quot;",
            "created_at": "2026-07-13T23:08:57+08:00",
        },
    ))
    repo.replace_current_comments("A", [
        CommentRecord(
            "C",
            "A",
            None,
            {
                "text": "抽到新角色 [语音评论3&quot;]",
                "created_at": "5分钟前",
            },
            "now",
        )
    ])

    post = ArchiveRenderSnapshot.from_repository(repo).posts[0]

    assert post["created_at"] == "2026-07-14 16:38"
    assert post["text"] == '正文 "内容"'
    assert post["retweeted_payload"]["created_at"] == "2026-07-13 23:08"
    assert post["retweeted_payload"]["text"] == '转发 "内容"'
    assert post["comments"][0]["created_at"] == "5分钟前"
    assert post["comments"][0]["text"] == "抽到新角色 语音评论（3秒）"


def test_snapshot_associates_comments_media_live_pairs_and_revisions(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    repo.replace_current_comments("A", [
        CommentRecord("C1", "A", None, {"text": "评论", "user_name": "甲", "local_image": "media/c.jpg"}, "now"),
        CommentRecord("C2", "A", "C1", {"text": "回复", "source": "iPhone"}, "now"),
    ])
    repo.upsert_media(MediaRecord("post", "A", "live_photo", 3, "remote-video", "media/A_live_04.mov"))
    repo.upsert_media(MediaRecord("post", "A", "live_photo_thumbnail", 3, "remote-image", "media/A_live_04.jpg"))
    repo.upsert_media(MediaRecord("comment", "C1", "image", 0, "remote-comment", "media/c.jpg"))
    repo.apply_post_change(_post("A", "2026-07-14T01:00:00+00:00", text="当前正文"))

    post = ArchiveRenderSnapshot.from_repository(repo).posts[0]

    assert post["comments"][1]["parent_id"] == "C1"
    assert post["comments"][0]["media"][0]["local_path"] == "media/c.jpg"
    assert post["media"][0] == {
        "kind": "live_photo", "position": 3,
        "image_path": "media/A_live_04.jpg", "video_path": "media/A_live_04.mov",
        "image_url": "media/A_live_04.jpg", "video_url": "media/A_live_04.mov",
    }
    assert post["revisions"][0]["payload"]["text"] == "正文 A"


@pytest.mark.parametrize("unsafe", ["/tmp/a.jpg", "../a.jpg", "media\\a.jpg", "media/../a.jpg", "media/a\x00.jpg"])
def test_snapshot_rejects_unsafe_local_media_paths(tmp_path, unsafe):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    repo._connection.execute(
        "INSERT INTO media(owner_type,owner_id,role,position,remote_url,local_path,sha256) VALUES(?,?,?,?,?,?,?)",
        ("post", "A", "image", 0, "remote", unsafe, ""),
    )
    with pytest.raises(ArchiveError, match="媒体路径"):
        ArchiveRenderSnapshot.from_repository(repo)


def test_archive_data_js_is_safe_deterministic_json_not_string_concatenation(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "", text="</script><script>坏\u2028行\u2029尾"))
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, path: path.write_bytes(b"pdf"))

    source = (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    assert source.startswith("window.__WEISHUSHU_ARCHIVE__ = {")
    assert source.endswith(";\n")
    assert "</script>" not in source
    assert "\\u003c/script>" in source
    assert "\u2028" not in source and "\u2029" not in source
    payload = json.loads(source.removeprefix("window.__WEISHUSHU_ARCHIVE__ = ").removesuffix(";\n"))
    assert payload["schema"] == 1
    assert payload["user"]["screen_name"] == "固定名字"


def test_render_all_creates_fixed_outputs_and_atomic_failure_preserves_old(tmp_path, monkeypatch):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    renderer = ArchiveRenderer(repo)
    paths = renderer.render_all(root, render_pdf=lambda _html, path: path.write_bytes(b"pdf"))
    assert paths == {
        "html": root / "微博书.html", "pdf": root / "微博书.pdf",
        "markdown": root / "微博书.md", "data": root / "data" / "archive-data.js",
    }
    old = (root / "微博书.html").read_bytes()
    monkeypatch.setattr(renderer, "_render_html", lambda _snapshot: (_ for _ in ()).throw(OSError("失败")))
    with pytest.raises(OSError, match="失败"):
        renderer.render_all(root, render_pdf=lambda _html, path: path.write_bytes(b"new"))
    assert (root / "微博书.html").read_bytes() == old
    assert not list(root.rglob("*.tmp"))


def test_render_all_rejects_empty_pdf_instead_of_claiming_success(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))

    with pytest.raises(ArchiveError, match="PDF"):
        ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, _path: None)
    assert not (root / "微博书.pdf").exists()


def test_archive_renderer_uses_independent_interactive_and_print_templates(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    interactive_marker = "INTERACTIVE-TEMPLATE-MARKER"
    print_marker = "PRINT-TEMPLATE-MARKER"
    renderer = ArchiveRenderer(repo)
    renderer.env.loader = DictLoader({
        "book_interactive.html": (
            f"<html><head></head><body>{interactive_marker}"
            '<script src="data/archive-data.js"></script></body></html>'
        ),
        "book.html": (
            f"<html><head></head><body>{print_marker}"
            '<script src="data/archive-data.js"></script></body></html>'
        ),
    })

    def inspect_print_html(print_html, pdf):
        source = print_html.read_text(encoding="utf-8")
        print_data_uri = (
            print_html.parent / "data" / "archive-print-data.js"
        ).resolve().as_uri()
        assert print_marker in source
        assert interactive_marker not in source
        assert f'<base href="{root.resolve().as_uri()}/">' in source
        assert f'<script src="{print_data_uri}"></script>' in source
        pdf.write_bytes(b"pdf")

    renderer.render_all(root, render_pdf=inspect_print_html)

    published = (root / "微博书.html").read_text(encoding="utf-8")
    assert interactive_marker in published
    assert print_marker not in published


def test_interactive_lightbox_css_respects_all_mobile_safe_areas():
    template = (
        Path(__file__).resolve().parents[1]
        / "weibo_book"
        / "templates"
        / "book_interactive.html"
    ).read_text(encoding="utf-8")
    lightbox_rule = template.split(".lightbox{", 1)[1].split("}", 1)[0]

    assert "box-sizing:border-box" in lightbox_rule
    assert (
        "padding:env(safe-area-inset-top) env(safe-area-inset-right) "
        "env(safe-area-inset-bottom) env(safe-area-inset-left)"
    ) in lightbox_rule


def test_html_is_file_protocol_offline_mobile_archive_with_required_controls(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    for index in range(21):
        repo.upsert_post(_post(f"P{index:02d}", f"2026-07-{(index % 9) + 1:02d}T01:00:00+00:00"))
    repo.upsert_media(MediaRecord("post", "P00", "video", 0, "remote", "media/v.mp4"))
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, path: path.write_bytes(b"pdf"))
    html = (root / "微博书.html").read_text(encoding="utf-8")

    for token in (
        "微博正文", "微博书", "data-view=\"feed\"", "data-view=\"detail\"",
        "data-media-lightbox", 'el("video")', "data-live-photo", "dataset.revisions",
        'el("img","comment-image")', 'data-action="newer-window"',
        'data-action="older-window"', 'data-action="latest-window"',
        "function renderTimelineWindow",
        "#post-", "prefers-reduced-motion", "role=\"dialog\"", "aria-modal=\"true\"",
        "preload=\"metadata\"", "data-action=\"zoom-in\"", "data-action=\"zoom-out\"",
        "function renderAllPosts", "selected.forEach", "__WEISHUSHU_PRINT_READY__",
    ):
        assert token in html
    assert '<script src="data/archive-data.js"></script>' in html
    forbidden = ("fetch(", "XMLHttpRequest", "WebSocket", "http://", "https://", "Cookie")
    assert all(value not in html for value in forbidden)
    assert "addEventListener(\"click\"" not in html or "action-item" not in html
    assert 'data-bid="P00"' not in html  # 微博正文只从安全 JSON 经 DOM API 创建


def test_generator_exposes_interactive_archive_without_changing_public_model_signatures(tmp_path):
    from weibo_book.generator import BookGenerator

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    paths = BookGenerator(root).generate_interactive_archive(
        repo, render_pdf=lambda _html, path: path.write_bytes(b"pdf")
    )
    assert paths["html"].name == "微博书.html"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fail_position", [1, 2, 3, 4])
def test_four_file_publish_rolls_back_every_replace_position(
    tmp_path, monkeypatch, existing, fail_position
):
    import weibo_book.archive.render_snapshot as module
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("NEW", ""))
    targets = [root / "微博书.html", root / "微博书.pdf", root / "微博书.md", root / "data" / "archive-data.js"]
    old = {}
    if existing:
        for index, target in enumerate(targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            old[target] = f"old-{index}".encode()
            target.write_bytes(old[target])

    real_replace = module.os.replace
    publishes = 0
    failed = False

    def fail_one(source, destination, *args, **kwargs):
        nonlocal publishes, failed
        source_path, destination_path = Path(source), Path(destination)
        if ".render-stage-" in source_path.as_posix() and destination_path in targets and "backup" not in source_path.parts:
            publishes += 1
            if publishes == fail_position and not failed:
                failed = True
                raise OSError(f"publish-{fail_position}")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", fail_one)
    with pytest.raises(OSError, match=f"publish-{fail_position}"):
        ArchiveRenderer(repo).render_all(
            root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"new-pdf")
        )

    for target in targets:
        if existing:
            assert target.read_bytes() == old[target]
        else:
            assert not target.exists()
    assert not list((root / "data").glob(".render-stage-*"))


def test_pdf_callback_reads_current_staging_data_not_old_published_data(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("CURRENT", "", text="本次正文"))
    (root / "data" / "archive-data.js").write_text("OLD-DATA", encoding="utf-8")
    seen = {}

    def inspect_staging(html_path, pdf_path):
        seen["html"] = html_path.read_text(encoding="utf-8")
        seen["data"] = (html_path.parent / "data" / "archive-print-data.js").read_text(encoding="utf-8")
        seen["same_tree"] = pdf_path.parent == html_path.parent
        pdf_path.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect_staging)

    assert seen["same_tree"] is True
    assert "<base href=" in seen["html"]
    assert (root.resolve().as_uri() + "/") in seen["html"]
    assert (root / "data" / "archive-data.js").resolve().as_uri() not in seen["html"]
    assert "archive-print-data.js" in seen["html"]
    assert "本次正文" in seen["data"]
    assert "OLD-DATA" not in seen["data"]
    published = (root / "微博书.html").read_text(encoding="utf-8")
    assert "<base href=" not in published
    assert root.resolve().as_uri() not in published


@pytest.mark.parametrize(
    "unsafe",
    ["file:a.jpg", "data:a.jpg", "https:/a.jpg", "other/a.jpg", "media/a:b.jpg", "media//a.jpg"],
)
def test_snapshot_requires_media_first_segment_and_rejects_schemes(tmp_path, unsafe):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    repo._connection.execute(
        "INSERT INTO media(owner_type,owner_id,role,position,remote_url,local_path,sha256) VALUES(?,?,?,?,?,?,?)",
        ("post", "A", "image", 0, "remote", unsafe, ""),
    )
    with pytest.raises(ArchiveError, match="媒体路径"):
        ArchiveRenderSnapshot.from_repository(repo)


def test_payload_projection_drops_unknown_credentials_at_every_level(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.archive.schema import PostRevisionRecord

    repo, root = _repo(tmp_path)
    secret = {"Cookie": "SUBP=secret", "authorization": "Bearer credential-value", "unknown": {"cookie": "x"}}
    repo.upsert_post(_post(
        "A", "", retweeted_payload={"text": "引用正文", **secret},
        link_card_payload={"title": "标题", "description": "描述", **secret},
    ))
    repo.replace_current_comments("A", [CommentRecord("C", "A", None, {"text": "评论", **secret}, "now")])
    repo.add_post_revision(PostRevisionRecord("A", 1, "now", {"text": "旧正文", **secret}, "hash"))
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))

    combined = (root / "微博书.html").read_text(encoding="utf-8") + (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    for forbidden in ("cookie", "authorization", "bearer", "subp", "secret", "credential-value"):
        assert forbidden not in combined.lower()
    assert "引用正文" in combined and "旧正文" in combined


def test_incomplete_live_photo_degrades_without_empty_live_paths(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    repo.upsert_media(MediaRecord("post", "A", "live_photo", 0, "remote", "media/only.mov"))
    repo.upsert_media(MediaRecord("post", "A", "live_photo_thumbnail", 1, "remote", "media/only.jpg"))

    media = ArchiveRenderSnapshot.from_repository(repo).posts[0]["media"]

    assert media == (
        {"kind": "video", "position": 0, "local_path": "media/only.mov", "browser_url": "media/only.mov", "cover_path": "", "cover_url": ""},
        {"kind": "image", "position": 1, "local_path": "media/only.jpg", "browser_url": "media/only.jpg"},
    )
    assert all(item["kind"] != "live_photo" for item in media)


def test_print_mode_detail_header_and_lightbox_focus_contracts_are_explicit():
    source = (Path(__file__).resolve().parents[1] / "weibo_book" / "templates" / "book.html").read_text(encoding="utf-8")
    generator = (Path(__file__).resolve().parents[1] / "weibo_book" / "generator.py").read_text(encoding="utf-8")

    for token in (
        "function renderAllPosts", "print-mode", "__WEISHUSHU_PRINT_READY__",
        "DOMContentLoaded", "function postHeader", "state.lightboxTrigger",
        'event.key==="Tab"', "aria-hidden", "inert", ".focus()",
    ):
        assert token in source
    assert 'wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")' in generator


def test_print_html_uses_escaped_file_uris_without_copying_media(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    root = tmp_path / "含 空格" / "归档"
    repo = ArchiveRepository.create(root, "10001", "固定名字")
    repo.upsert_post(_post("A", ""))
    media = root / "media" / "one.png"
    media.parent.mkdir()
    media.write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082")
    )
    repo.upsert_media(MediaRecord("post", "A", "image", 0, "remote", "media/one.png"))
    seen = {}

    def inspect(print_html, pdf):
        source = print_html.read_text(encoding="utf-8")
        seen["base"] = root.resolve().as_uri() + "/" in source
        seen["data_uri"] = (print_html.parent / "data" / "archive-print-data.js").resolve().as_uri() in source
        seen["media_not_copied"] = not (print_html.parent / "media").exists()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert seen == {"base": True, "data_uri": True, "media_not_copied": True}
    assert "file:" not in (root / "微博书.html").read_text(encoding="utf-8")
    assert "file:" not in (root / "data" / "archive-data.js").read_text(encoding="utf-8")


def test_windows_file_uri_encodes_spaces_and_chinese_without_guessing():
    from weibo_book.archive.render_snapshot import _as_file_uri

    uri = _as_file_uri(PureWindowsPath("C:/微博 书/data/archive-data.js"))

    assert uri == "file:///C:/%E5%BE%AE%E5%8D%9A%20%E4%B9%A6/data/archive-data.js"


def test_render_rejects_symlink_archive_root(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    require_symlink_capability(target_is_directory=True)
    repo, root = _repo(tmp_path)
    alias = tmp_path / "archive-alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(ArchiveError, match="归档根目录"):
        ArchiveRenderer(repo).render_all(
            alias, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
        )


def test_forward_and_link_cards_project_only_local_media_for_shared_renderer(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "A", "", link_card_payload={
            "title": "主引用", "description": "主引用描述", "local_image": "media/main-card.jpg",
            "url": "https://remote.invalid/main",
        }, retweeted_payload={
            "bid": "R", "uid": "200", "user_name": "转发作者", "text": "转发正文",
            "source": "转发来源", "ip_location": "发布于 转发地区",
            "media": [
                {"type": "image", "position": 0, "local_path": "media/forward.jpg"},
                {"type": "video", "position": 1, "local_path": "media/forward.mp4", "video_cover": "media/cover.jpg"},
            ],
            "link_card": {"title": "嵌套引用", "description": "嵌套描述", "local_image": "media/nested.jpg", "url": "https://remote.invalid/nested"},
        },
    ))
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    data = (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    html = (root / "微博书.html").read_text(encoding="utf-8")

    for value in (
        "转发作者", "转发正文", "转发来源", "发布于 转发地区",
        "media/forward.jpg", "media/forward.mp4", "media/cover.jpg",
        "主引用", "media/main-card.jpg", "嵌套引用", "media/nested.jpg",
    ):
        assert value in data
    for token in (
        "function renderMediaItems", "renderMediaItems(post.media", "renderMediaItems(retweet.media",
        "function renderLinkCard", "card.browser_url", "video.controls=true",
    ):
        assert token in html
    assert "https://remote.invalid/main" in data
    assert "remote.invalid" not in html
    assert "fetch(" not in html and "XMLHttpRequest" not in html


def test_real_playwright_prints_all_twenty_one_posts_and_loads_local_png(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    for index in range(21):
        repo.upsert_post(_post(f"P{index:02d}", f"2026-07-14T{index:02d}:00:00+00:00"))
    image = root / "media" / "one.png"
    image.parent.mkdir()
    image.write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082")
    )
    repo.upsert_media(MediaRecord("post", "P00", "image", 0, "remote", "media/one.png"))
    encoded = root / "media" / "%2e%2e.png"
    encoded.write_bytes(image.read_bytes())
    repo.upsert_media(MediaRecord("post", "P01", "image", 0, "remote", "media/%2e%2e.png"))
    observed = {}

    def real_render(print_html, pdf):
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(5000)
            failed = []
            page.on("requestfailed", lambda request: failed.append(request.url))
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            page.wait_for_function("() => Array.from(document.images).filter(image => image.hasAttribute('src')).every(image => image.complete && image.naturalWidth > 0)")
            observed["cards"] = page.locator(".feed-card").count()
            observed["image_widths"] = page.locator(
                ".feed-card .pdf-media-link img"
            ).evaluate_all("images => images.map(image => image.naturalWidth)")
            observed["failed"] = failed
            page.pdf(path=str(pdf), format="A4", print_background=True)
            browser.close()

    ArchiveRenderer(repo).render_all(root, render_pdf=real_render)

    assert observed == {"cards": 21, "image_widths": [1, 1], "failed": []}


@pytest.mark.parametrize("viewport_width", [375, 390, 430, 680])
def test_archive_html_never_overflows_mobile_viewport(tmp_path, viewport_width):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "BID1",
        "2026-07-14T16:38:01+08:00",
        source="来自" + "A" * 180,
        text="用于检查手机窄屏排版的微博正文",
        link_card_payload={
            "title": "B" * 180,
            "description": "C" * 180,
        },
    ))
    ArchiveRenderer(repo).render_all(
        root,
        render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"),
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport_width, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        dimensions = page.evaluate("""() => ({
            viewport: window.innerWidth,
            body: document.body.scrollWidth,
            cardRight: Math.ceil(document.querySelector('.feed-card').getBoundingClientRect().right),
        })""")
        browser.close()

    assert dimensions["body"] <= dimensions["viewport"]
    assert dimensions["cardRight"] <= dimensions["viewport"]


def test_print_archive_omits_technical_paths_empty_comments_and_symbol_placeholders(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T16:38:01+08:00"))
    for role, position, local_path in (
        ("video", 0, "media/posts/video.mp4"),
        ("video_cover", 0, "media/posts/video-cover.jpg"),
        ("live_photo", 1, "media/posts/live.mov"),
        ("live_photo_thumbnail", 1, "media/posts/live.jpg"),
    ):
        repo.upsert_media(MediaRecord("post", "BID1", role, position, "remote", local_path))
    observed = {}

    def inspect(print_html, pdf):
        observed["source"] = print_html.read_text(encoding="utf-8")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            observed["text"] = page.locator("body").inner_text()
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert "本地文件：" not in observed["text"]
    assert "归档中没有评论" not in observed["text"]
    assert "↗" not in observed["text"]
    assert "□" not in observed["text"]
    assert "♡" not in observed["text"]
    assert ".pdf-appendices" not in observed["source"]


def test_print_query_renders_only_requested_post_batch(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    media = root / "media" / "one.png"
    media.parent.mkdir()
    media.write_bytes(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082")
    )
    for index in range(25):
        bid = f"P{index:02d}"
        repo.upsert_post(_post(bid, f"2026-07-14T{index % 24:02d}:00:00+00:00"))
        repo.upsert_media(
            MediaRecord("post", bid, "image", 0, "remote", "media/one.png")
        )
    observed = {}

    def inspect(print_html, pdf):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                print_html.resolve().as_uri()
                + "?print=1&printStart=20&printLimit=5",
                wait_until="load",
            )
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            observed["cards"] = page.locator(".feed-card").count()
            observed["appendices"] = page.locator(".pdf-image-appendix").count()
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert observed["cards"] == 5
    assert observed["appendices"] == 0


def test_print_page_uses_generated_thumbnail_instead_of_original_image(tmp_path):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    original = root / "media" / "large.png"
    original.parent.mkdir()
    Image.new("RGB", (1200, 900), "orange").save(original)
    repo.upsert_media(
        MediaRecord("post", "A", "image", 0, "remote", "media/large.png")
    )
    observed = {}

    def inspect(print_html, pdf):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            image = page.locator(".feed-card .pdf-media-link img")
            observed["src"] = image.get_attribute("src")
            observed["width"] = image.evaluate("node => node.naturalWidth")
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert "/print-media/" in observed["src"]
    assert observed["width"] <= 640


def test_browser_url_quotes_each_segment_and_literal_encoded_dotdot_stays_local(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    path = root / "media" / "%2e%2e 图.png"
    path.parent.mkdir()
    path.write_bytes(b"local")
    repo.upsert_media(MediaRecord("post", "A", "image", 0, "remote", "media/%2e%2e 图.png"))

    item = ArchiveRenderSnapshot.from_repository(repo).posts[0]["media"][0]

    assert item["local_path"] == "media/%2e%2e 图.png"
    assert item["browser_url"] == "media/%252e%252e%20%E5%9B%BE.png"
    assert (root / item["local_path"]).resolve().is_relative_to(root.resolve())


@pytest.mark.parametrize("replace_position", [1, 2, 3, 4])
def test_next_render_recovers_after_process_exit_between_replaces(tmp_path, replace_position):
    root = tmp_path / "archive"
    repo = ArchiveRepository.create(root, "10001", "固定名字")
    repo.upsert_post(_post("A", "", text="崩溃后新正文"))
    targets = [root / "微博书.html", root / "微博书.pdf", root / "微博书.md", root / "data" / "archive-data.js"]
    for index, target in enumerate(targets):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"OLD-{index}".encode())
    repo.close()
    code = r'''
import os
from pathlib import Path
import weibo_book.archive.render_snapshot as module
from weibo_book.archive.repository import ArchiveRepository
root=Path(os.environ["ARCHIVE_ROOT"]); position=int(os.environ["FAIL_POSITION"])
repo=ArchiveRepository.open(root,"10001"); real=module.os.replace
targets={root/"微博书.html",root/"微博书.pdf",root/"微博书.md",root/"data"/"archive-data.js"}; count=0
def crash(source,destination):
    global count
    result=real(source,destination)
    if Path(destination) in targets and ".render-stage-" in Path(source).as_posix():
        count+=1
        if count==position: os._exit(73)
    return result
module.os.replace=crash
module.ArchiveRenderer(repo).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"NEW-PDF"))
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ARCHIVE_ROOT": str(root), "FAIL_POSITION": str(replace_position)},
    )
    assert result.returncode == 73

    repo = ArchiveRepository.open(root, "10001")
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"FINAL-PDF"))

    assert "崩溃后新正文" in (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    assert (root / "微博书.pdf").read_bytes() == b"FINAL-PDF"
    assert not list((root / "data").glob(".render-stage-*"))
    assert not (root / "data" / ".weishushu-render-state.json").exists()


@pytest.mark.parametrize("restore_position", [1, 2, 3, 4])
def test_next_render_recovers_after_process_exit_during_each_restore(tmp_path, restore_position):
    root = tmp_path / "archive"
    repo = ArchiveRepository.create(root, "10001", "固定名字")
    repo.upsert_post(_post("A", "", text="最终正文"))
    for index, target in enumerate((
        root / "微博书.html", root / "微博书.pdf", root / "微博书.md",
        root / "data" / "archive-data.js",
    )):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"OLD-{index}".encode())
    repo.close()
    publish_crash = r'''
import os
from pathlib import Path
import weibo_book.archive.render_snapshot as module
from weibo_book.archive.repository import ArchiveRepository
root=Path(os.environ["ARCHIVE_ROOT"]); repo=ArchiveRepository.open(root,"10001"); real=module.os.replace
targets={root/"微博书.html",root/"微博书.pdf",root/"微博书.md",root/"data"/"archive-data.js"}
def crash(source,destination,*args,**kwargs):
    result=real(source,destination,*args,**kwargs)
    if Path(destination) in targets and ".render-stage-" in Path(source).as_posix(): os._exit(73)
    return result
module.os.replace=crash
module.ArchiveRenderer(repo).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"NEW-PDF"))
'''
    first = subprocess.run(
        [sys.executable, "-c", publish_crash], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ARCHIVE_ROOT": str(root)},
    )
    assert first.returncode == 73

    restore_crash = r'''
import os
from pathlib import Path
import weibo_book.archive.render_snapshot as module
from weibo_book.archive.repository import ArchiveRepository
root=Path(os.environ["ARCHIVE_ROOT"]); position=int(os.environ["RESTORE_POSITION"]); count=0
original=module.ArchiveRenderer._restore_backup.__func__
def crash(cls,backup,target):
    global count
    original(cls,backup,target); count+=1
    if count==position: os._exit(74)
module.ArchiveRenderer._restore_backup=classmethod(crash)
repo=ArchiveRepository.open(root,"10001")
module.ArchiveRenderer(repo).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"UNREACHED"))
'''
    second = subprocess.run(
        [sys.executable, "-c", restore_crash], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "ARCHIVE_ROOT": str(root), "RESTORE_POSITION": str(restore_position)},
    )
    assert second.returncode == 74

    repo = ArchiveRepository.open(root, "10001")
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"FINAL-PDF"))
    assert "最终正文" in (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    assert (root / "微博书.pdf").read_bytes() == b"FINAL-PDF"
    assert not (root / "data" / ".weishushu-render-state.json").exists()
    assert not list((root / "data").glob(".render-stage-*"))


def test_data_symlink_is_rejected_without_writing_external_directory(tmp_path):
    require_symlink_capability(target_is_directory=True)
    repo, root = _repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    data = root / "data"
    moved = root / "real-data"
    repo.close()
    data.rename(moved)
    data.symlink_to(external, target_is_directory=True)

    from weibo_book.archive.render_snapshot import ArchiveRenderer
    with pytest.raises(ArchiveError, match="data"):
        ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    assert list(external.iterdir()) == []


def test_fixed_target_hardlink_is_rejected(tmp_path):
    repo, root = _repo(tmp_path)
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, root / "微博书.html")
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    with pytest.raises(ArchiveError, match="单链接"):
        ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    assert outside.read_text(encoding="utf-8") == "outside"


def test_real_playwright_revision_redraw_and_lightbox_group_do_not_request_remote(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.archive.schema import PostRevisionRecord

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "A", "2026-07-14T01:00:00+00:00", text="当前正文", source="当前来源",
        ip_location="当前地区", link_card_payload={"title":"当前链接","url":"https://example.invalid/current"},
    ))
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082")
    for position, name in enumerate(("main-1.png", "main-2.png")):
        path=root/"media"/name; path.parent.mkdir(exist_ok=True); path.write_bytes(png)
        repo.upsert_media(MediaRecord("post","A","image",position,"remote",f"media/{name}"))
    old=root/"media"/"old.png"; old.write_bytes(png)
    repo.add_post_revision(PostRevisionRecord("A",1,"2026-07-15T00:00:00+00:00",{
        "bid":"A","uid":"10001","text":"历史正文","created_at":"2026-07-13T01:00:00+00:00",
        "source":"历史来源","ip_location":"历史地区","visibility":"unavailable","is_pinned":True,
        "link_card_payload":{"title":"历史链接","url":"https://example.invalid/old"},
        "media_signature":[{"type":"image","position":0,"local_path":"media/old.png"}],
    },"hash"))
    ArchiveRenderer(repo).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"pdf"))
    observed={}
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True); page=browser.new_page(); page.set_default_timeout(5000); requests=[]
        page.on("request",lambda request: requests.append(request.url))
        page.goto((root/"微博书.html").resolve().as_uri(),wait_until="load")
        page.locator('[data-bid="A"] .card-head').click()
        page.locator('[data-view="detail"] [data-media-group="A:current:main"]').first.click()
        page.locator('[data-action="lightbox-next"]').click()
        observed["next"] = page.locator("[data-lightbox-image]").get_attribute("src")
        page.keyboard.press("Escape")
        page.locator('[data-revision-index="0"]').click()
        observed["old"] = page.locator('[data-view="detail"]').inner_text()
        observed["old_href"] = page.locator('[data-view="detail"] a').get_attribute("href")
        old_image = page.locator('[data-view="detail"] [data-local-image]')
        observed["old_image"] = old_image.get_attribute("data-local-image")
        observed["old_group"] = old_image.get_attribute("data-media-group")
        old_image.click()
        observed["old_lightbox"] = page.locator("[data-lightbox-image]").get_attribute("src")
        page.keyboard.press("Escape")
        page.locator('[data-revision-current]').click()
        observed["current"] = page.locator('[data-view="detail"]').inner_text()
        browser.close()
    assert observed["next"].endswith("media/main-2.png")
    assert all(value in observed["old"] for value in ("历史正文","历史来源","历史地区","置顶","当前不可见"))
    assert observed["old_href"] == "https://example.invalid/old"
    assert observed["old_image"] == "media/old.png"
    assert observed["old_group"] == "A:rev-1:main"
    assert observed["old_lightbox"].endswith("media/old.png")
    assert all(value in observed["current"] for value in ("当前正文","当前来源","当前地区"))
    assert not any(url.startswith("https://example.invalid") for url in requests)


def test_feed_lightbox_controls_and_live_photo_render_without_opening_detail(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    for position, relative_path in enumerate(
        ("media/feed-first.png", "media/feed-second.png")
    ):
        _write_local_png(root, relative_path)
        repo.upsert_media(
            MediaRecord("post", "A", "image", position, "remote", relative_path)
        )
    _add_live_photo_pair(
        repo,
        root,
        "A",
        2,
        "media/feed-live.png",
        "media/feed-live.mov",
    )
    normal_video = root / "media/feed-video.mp4"
    normal_video.write_bytes(b"local-normal-video")
    repo.upsert_media(
        MediaRecord("post", "A", "video", 3, "remote-video", "media/feed-video.mp4")
    )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.emulate_media(reduced_motion="reduce")
        page.set_default_timeout(5000)
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        card = page.locator('[data-bid="A"]')
        grid = card.locator(".media-grid")
        images = grid.locator("[data-local-image]")
        first_image = images.nth(0)
        lightbox_image = page.locator("[data-lightbox-image]")
        lightbox_image.evaluate(
            "image => image.style.setProperty('--scale', 'empty-group-sentinel')"
        )
        page.locator('[data-action="lightbox-next"]').evaluate(
            "button => button.click()"
        )
        assert lightbox_image.evaluate(
            "image => image.style.getPropertyValue('--scale')"
        ) == "empty-group-sentinel"

        first_image.click()
        lightbox = page.locator("[data-media-lightbox]")
        assert lightbox.is_visible()
        assert page.locator('[data-view="detail"]').is_hidden()
        assert page.locator('[data-view="feed"]').is_visible()
        assert page.locator("[data-lightbox-image]").get_attribute("src").endswith(
            "media/feed-first.png"
        )

        page.locator('[data-action="lightbox-next"]').click()
        assert page.locator("[data-lightbox-image]").get_attribute("src").endswith(
            "media/feed-second.png"
        )
        page.locator('[data-action="zoom-in"]').click()
        assert page.locator("[data-lightbox-image]").evaluate(
            "image => image.style.getPropertyValue('--scale')"
        ) == "1.25"
        stage = page.locator(".lightbox-stage")
        bounds = stage.bounding_box()
        assert bounds is not None
        start_x = bounds["x"] + bounds["width"] / 2
        start_y = bounds["y"] + bounds["height"] / 2
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x + 32, start_y + 24)
        page.mouse.up()
        assert page.locator("[data-lightbox-image]").evaluate(
            "image => [image.style.getPropertyValue('--tx'), image.style.getPropertyValue('--ty')]"
        ) == ["32px", "24px"]

        page.locator('[data-action="lightbox-close"]').click()
        assert lightbox.is_hidden()
        assert first_image.evaluate("image => document.activeElement === image") is True
        assert page.locator('[data-view="detail"]').is_hidden()

        first_image.click()
        assert lightbox.is_visible()
        page.keyboard.press("Escape")
        assert lightbox.is_hidden()
        assert first_image.evaluate("image => document.activeElement === image") is True
        assert page.locator('[data-view="detail"]').is_hidden()
        assert grid.locator(":scope > .video-shell").count() == 4
        live_photo = grid.locator("[data-live-photo]")
        live_image = live_photo.locator("img")
        live_video = live_photo.locator("video")
        normal_video = grid.locator('video[aria-label="微博视频"]')
        assert live_photo.count() == 1
        assert live_image.evaluate("image => getComputedStyle(image).display") == "block"
        assert live_video.evaluate("video => getComputedStyle(video).display") == "none"
        assert normal_video.get_attribute("hidden") is None
        assert normal_video.evaluate("video => getComputedStyle(video).display") == "block"

        _install_valid_webm(live_video)
        live_photo.click()
        assert live_image.evaluate("image => getComputedStyle(image).display") == "none"
        assert live_video.evaluate("video => getComputedStyle(video).display") == "block"
        assert normal_video.evaluate("video => getComputedStyle(video).display") == "block"

        live_photo.click()
        assert live_image.evaluate("image => getComputedStyle(image).display") == "block"
        assert live_video.evaluate("video => getComputedStyle(video).display") == "none"
        assert normal_video.evaluate("video => getComputedStyle(video).display") == "block"
        browser.close()


def test_large_lightbox_image_does_not_cover_pointer_controls(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    for position, (relative_path, color) in enumerate(
        (
            ("media/large-first.png", (190, 40, 40)),
            ("media/large-second.png", (40, 90, 190)),
        )
    ):
        _write_large_local_png(root, relative_path, color)
        repo.upsert_media(
            MediaRecord("post", "A", "image", position, "remote", relative_path)
        )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844}, has_touch=True
        )
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        trigger = page.locator('[data-bid="A"] [data-local-image]').first
        trigger.click()
        lightbox = page.locator("[data-media-lightbox]")
        lightbox_image = page.locator("[data-lightbox-image]")
        page.wait_for_function(
            """() => {
                const image = document.querySelector('[data-lightbox-image]');
                return image.complete && image.naturalWidth === 1200;
            }"""
        )

        def control_center(action: str) -> tuple[float, float]:
            control = page.locator(f'[data-action="{action}"]')
            bounds = control.bounding_box()
            assert bounds is not None
            x = bounds["x"] + bounds["width"] / 2
            y = bounds["y"] + bounds["height"] / 2
            hit = page.evaluate(
                """([x, y]) => {
                    const node = document.elementFromPoint(x, y);
                    const control = node && node.closest('[data-action]');
                    return {
                        action: control ? control.dataset.action : null,
                        tag: node ? node.tagName : null,
                        className: node ? node.className : null,
                    };
                }""",
                [x, y],
            )
            assert hit["action"] == action, f"{action} 中心被其他元素覆盖: {hit}"
            return x, y

        centers = {
            action: control_center(action)
            for action in (
                "lightbox-close",
                "lightbox-prev",
                "lightbox-next",
                "zoom-out",
                "zoom-in",
            )
        }
        first_source = lightbox_image.get_attribute("src")
        page.mouse.click(*centers["lightbox-next"])
        second_source = lightbox_image.get_attribute("src")
        assert second_source != first_source
        page.touchscreen.tap(*centers["lightbox-prev"])
        assert lightbox_image.get_attribute("src") == first_source

        page.mouse.click(*centers["zoom-in"])
        assert lightbox_image.evaluate(
            "image => image.style.getPropertyValue('--scale')"
        ) == "1.25"
        page.mouse.click(*centers["zoom-out"])
        assert lightbox_image.evaluate(
            "image => image.style.getPropertyValue('--scale')"
        ) == "1"

        stage = page.locator(".lightbox-stage")
        stage_bounds = stage.bounding_box()
        assert stage_bounds is not None
        start_x = stage_bounds["x"] + stage_bounds["width"] / 2
        start_y = stage_bounds["y"] + stage_bounds["height"] / 2
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.mouse.move(start_x + 31, start_y + 19)
        page.mouse.up()
        assert lightbox_image.evaluate(
            "image => [image.style.getPropertyValue('--tx'), image.style.getPropertyValue('--ty')]"
        ) == ["31px", "19px"]

        lightbox.focus()
        page.keyboard.press("Tab")
        assert page.locator('[data-action="lightbox-close"]').evaluate(
            "button => document.activeElement === button"
        ) is True
        page.mouse.click(*centers["lightbox-close"])
        assert lightbox.is_hidden()
        assert trigger.evaluate("node => document.activeElement === node") is True
        browser.close()


@pytest.mark.parametrize("interaction", ["mouse", "keyboard", "touch"])
def test_valid_live_photo_can_return_to_image_with_reduced_motion(
    tmp_path, interaction
):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    for position in (0, 2):
        relative_path = f"media/image-{position}.png"
        _write_local_png(root, relative_path)
        repo.upsert_media(
            MediaRecord("post", "A", "image", position, "remote", relative_path)
        )
    _add_live_photo_pair(
        repo, root, "A", 1, "media/live.png", "media/live.webm"
    )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.emulate_media(reduced_motion="reduce")
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        live_photo = page.locator('[data-bid="A"] [data-live-photo]')
        live_video = live_photo.locator("video")
        _install_valid_webm(live_video)

        def activate() -> None:
            if interaction == "mouse":
                live_photo.click()
            elif interaction == "keyboard":
                live_photo.focus()
                page.keyboard.press("Enter")
            else:
                live_photo.dispatch_event("click")

        assert live_photo.get_attribute("aria-label") == "播放实况照片"
        assert live_photo.get_attribute("aria-pressed") == "false"
        activate()
        assert live_video.evaluate("video => getComputedStyle(video).display") == "block"
        assert live_video.evaluate("video => video.paused") is False
        assert live_photo.get_attribute("aria-label") == "暂停实况照片"
        assert live_photo.get_attribute("aria-pressed") == "true"
        activate()
        assert live_photo.locator("img").evaluate(
            "image => getComputedStyle(image).display"
        ) == "block"
        assert live_video.evaluate("video => getComputedStyle(video).display") == "none"
        assert live_photo.get_attribute("aria-label") == "播放实况照片"
        assert live_photo.get_attribute("aria-pressed") == "false"
        browser.close()


def test_live_photo_play_rejection_rolls_back_without_pageerror(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    _add_live_photo_pair(repo, root, "A", 0, "media/live.png", "media/live.mov")
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        live_photo = page.locator('[data-bid="A"] [data-live-photo]')
        live_video = live_photo.locator("video")
        live_video.evaluate(
            """video => {
                video.play = () => new Promise((_resolve, reject) => {
                    window.__rejectLivePlay = reject;
                });
                video.pause = () => { window.__livePauseCount = (window.__livePauseCount || 0) + 1; };
            }"""
        )
        live_photo.click()
        page.evaluate("window.__rejectLivePlay(new DOMException('bad media', 'NotSupportedError'))")
        page.wait_for_function(
            "document.querySelector('[data-live-photo] video').hidden === true"
        )

        assert live_photo.locator("img").is_visible()
        assert live_video.is_hidden()
        assert live_photo.get_attribute("aria-label") == "播放实况照片"
        assert live_photo.get_attribute("aria-pressed") == "false"
        assert page.evaluate("window.__livePauseCount") == 1
        assert page_errors == []
        browser.close()


def test_late_play_rejection_does_not_override_newer_live_photo_state(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    _add_live_photo_pair(repo, root, "A", 0, "media/live.png", "media/live.mov")
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        live_photo = page.locator('[data-bid="A"] [data-live-photo]')
        live_video = live_photo.locator("video")
        live_video.evaluate(
            """video => {
                window.__liveAttempts = [];
                video.play = () => new Promise((resolve, reject) => {
                    window.__liveAttempts.push({resolve, reject});
                });
                video.pause = () => {};
            }"""
        )
        live_photo.click()
        live_photo.click()
        live_photo.click()
        page.evaluate(
            "window.__liveAttempts[0].reject(new DOMException('late', 'AbortError'))"
        )
        page.wait_for_timeout(50)

        assert live_video.is_visible()
        assert live_photo.locator("img").is_hidden()
        assert live_photo.get_attribute("aria-label") == "暂停实况照片"
        assert live_photo.get_attribute("aria-pressed") == "true"
        page.evaluate("window.__liveAttempts[1].resolve()")
        browser.close()


@pytest.mark.parametrize(
    ("image_count", "expected_layout", "expected_columns"),
    [
        (1, "single", 1),
        (2, "double", 2),
        (3, "triple", 3),
        (4, "quad", 2),
        (5, "nine", 3),
        (6, "nine", 3),
        (7, "nine", 3),
        (8, "nine", 3),
        (9, "nine", 3),
    ],
)
def test_mobile_media_grid_uses_weibo_layout_without_narrow_overflow(
    tmp_path, image_count, expected_layout, expected_columns
):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    for position in range(image_count):
        relative_path = f"media/grid-{position + 1}.png"
        image_path = root / relative_path
        image_path.parent.mkdir(parents=True, exist_ok=True)
        dimensions = (300, 1200) if image_count == 1 else (600, 420)
        Image.new("RGB", dimensions, (240, 126, 88)).save(image_path)
        repo.upsert_media(
            MediaRecord("post", "A", "image", position, "remote", relative_path)
        )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        grid = page.locator('[data-bid="A"] .media-grid')
        assert grid.get_attribute("data-count") == str(image_count)
        assert grid.get_attribute("data-layout") == expected_layout
        if image_count == 1:
            assert grid.get_attribute("data-single-kind") == "image"
        assert grid.locator(":scope > .video-shell").count() == image_count
        assert grid.evaluate(
            "grid => getComputedStyle(grid).gridTemplateColumns.split(' ').length"
        ) == expected_columns
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        ) is True
        if image_count == 1:
            image = grid.locator("img")
            assert image.evaluate("image => getComputedStyle(image).objectFit") == "contain"
            assert image.evaluate("image => image.getBoundingClientRect().height <= 420") is True
            assert image.evaluate(
                "image => image.getBoundingClientRect().width / "
                "image.getBoundingClientRect().height"
            ) == pytest.approx(300 / 1200, rel=0.01)
            geometry = grid.evaluate(
                """grid => {
                    const shell = grid.querySelector('.video-shell');
                    const button = shell.querySelector('.media-button');
                    const image = button.querySelector('img');
                    return [grid, shell, button, image].map(node => {
                        const rect = node.getBoundingClientRect();
                        return [rect.width, rect.height];
                    });
                }"""
            )
            for width, height in geometry[1:]:
                assert width == pytest.approx(geometry[0][0], abs=1)
                assert height == pytest.approx(geometry[0][1], abs=1)
            assert geometry[0][0] <= grid.evaluate("grid => grid.parentElement.clientWidth")
            assert geometry[0][1] <= 420
        else:
            assert grid.locator(".video-shell").first.evaluate(
                "shell => Math.abs(shell.getBoundingClientRect().height - "
                "shell.querySelector('img').getBoundingClientRect().height) < 1"
            ) is True
        browser.close()


def test_single_horizontal_image_container_matches_media_without_overflow(tmp_path):
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    image_path = root / "media/horizontal.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1200, 300), (90, 150, 190)).save(image_path)
    repo.upsert_media(
        MediaRecord("post", "A", "image", 0, "remote", "media/horizontal.png")
    )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        grid = page.locator('[data-bid="A"] .media-grid')
        geometry = grid.evaluate(
            """grid => {
                const shell = grid.querySelector('.video-shell');
                const button = shell.querySelector('.media-button');
                const image = button.querySelector('img');
                return [grid, shell, button, image].map(node => {
                    const rect = node.getBoundingClientRect();
                    return [rect.width, rect.height];
                });
            }"""
        )
        assert grid.get_attribute("data-single-kind") == "image"
        for width, height in geometry[1:]:
            assert width == pytest.approx(geometry[0][0], abs=1)
            assert height == pytest.approx(geometry[0][1], abs=1)
        assert geometry[0][0] / geometry[0][1] == pytest.approx(4, rel=0.01)
        assert geometry[0][0] <= grid.evaluate("grid => grid.parentElement.clientWidth")
        assert geometry[0][1] <= 420
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        ) is True
        browser.close()


@pytest.mark.parametrize("single_kind", ["video", "live_photo"])
def test_single_video_media_kinds_have_stable_local_container_geometry(
    tmp_path, single_kind
):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    if single_kind == "video":
        video_path = root / "media/single-video.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"local-video")
        repo.upsert_media(
            MediaRecord("post", "A", "video", 0, "remote", "media/single-video.mp4")
        )
    else:
        _add_live_photo_pair(
            repo,
            root,
            "A",
            0,
            "media/single-live.png",
            "media/single-live.mov",
        )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.emulate_media(reduced_motion="reduce")
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        grid = page.locator('[data-bid="A"] .media-grid')
        shell = grid.locator(":scope > .video-shell")
        assert grid.get_attribute("data-single-kind") == single_kind
        grid_box = grid.bounding_box()
        shell_box = shell.bounding_box()
        assert grid_box is not None and shell_box is not None
        assert shell_box["width"] == pytest.approx(grid_box["width"], abs=1)
        assert shell_box["height"] == pytest.approx(grid_box["height"], abs=1)
        assert grid_box["width"] <= grid.evaluate("grid => grid.parentElement.clientWidth")
        assert grid_box["height"] <= 420
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        ) is True
        if single_kind == "video":
            assert shell.locator('video[aria-label="微博视频"]').count() == 1
        else:
            live_photo = shell.locator("[data-live-photo]")
            assert live_photo.count() == 1
            _install_valid_webm(live_photo.locator("video"))
            live_photo.click()
            assert live_photo.locator("video").evaluate(
                "video => getComputedStyle(video).display"
            ) == "block"
        browser.close()


def test_pinned_mobile_feed_card_has_read_only_actions_and_responsive_width(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(
        _post(
            "PIN",
            "2026-07-14T01:00:00+00:00",
            source="iPhone",
            ip_location="发布于韩国",
            is_pinned=True,
            reposts_count=12,
            comments_count=34,
            likes_count=56,
        )
    )
    _write_local_png(root, "media/pinned-card.png")
    repo.upsert_media(
        MediaRecord("post", "PIN", "image", 0, "remote", "media/pinned-card.png")
    )
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        requests = []
        page_errors = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        card = page.locator('[data-bid="PIN"]')
        assert card.get_attribute("data-pinned") == "true"
        assert card.locator(":scope > .wb-pin-banner").inner_text() == "置顶"
        assert card.locator(".wb-post-header").count() == 1
        assert card.locator(".wb-post-identity").count() == 1
        assert card.locator(".wb-post-name").inner_text().startswith("固定名字")
        assert card.locator(".wb-post-meta").inner_text() == (
            "2026-07-14 01:00 · iPhone · 发布于韩国"
        )
        assert card.locator(".wb-post-body").count() == 1
        actions = card.locator(".wb-post-actions")
        assert actions.locator(":scope > .wb-post-action").count() == 3
        assert actions.locator("svg").count() == 3
        assert actions.locator("button, a").count() == 0
        assert actions.inner_text().splitlines() == ["转发 12", "评论 34", "赞 56"]
        assert card.evaluate(
            "card => (card.innerText.match(/置顶/g) || []).length"
        ) == 1

        for width in (375, 390, 430, 680, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate(
                "document.documentElement.scrollWidth === document.documentElement.clientWidth"
            ) is True
            shell_width = page.locator("[data-app-shell]").evaluate(
                "shell => shell.getBoundingClientRect().width"
            )
            assert shell_width <= (900 if width >= 900 else width)
            feed_width = page.locator(".feed-column").evaluate(
                "feed => feed.getBoundingClientRect().width"
            )
            assert feed_width <= 680
            if width <= 680:
                assert shell_width == pytest.approx(width, abs=1)
            else:
                shell_left = page.locator("[data-app-shell]").evaluate(
                    "shell => shell.getBoundingClientRect().left"
                )
                assert shell_left == pytest.approx((width - shell_width) / 2, abs=1)
            boundaries = card.evaluate(
                """card => {
                    const rect = node => {
                        const box = node.getBoundingClientRect();
                        return [box.left, box.right];
                    };
                    return {
                        shell: rect(document.querySelector('[data-app-shell]')),
                        card: rect(card),
                        grid: rect(card.querySelector('.media-grid')),
                        actions: rect(card.querySelector('.wb-post-actions')),
                    };
                }"""
            )
            shell_left, shell_right = boundaries["shell"]
            for left, right in (
                boundaries["card"], boundaries["grid"], boundaries["actions"]
            ):
                assert left >= shell_left - 1
                assert right <= shell_right + 1

        actions.locator(".wb-post-action").first.click()
        detail_view = page.locator('[data-view="detail"]')
        assert detail_view.is_visible()
        assert detail_view.evaluate(
            "view => (view.innerText.match(/置顶/g) || []).length"
        ) == 1
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        ) is True
        assert page_errors == []
        assert not any(url.startswith(("http://", "https://")) for url in requests)
        browser.close()


def test_renderer_uses_only_local_main_retweet_and_comment_avatars(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "", retweeted_payload={
        "uid": "200", "user_name": "转发作者", "user_avatar": "https://remote/avatar.jpg", "text": "转发"
    }))
    repo.replace_current_comments("A", [CommentRecord(
        "C", "A", None, {"text": "评论", "user_name": "评论者", "user_id": "300", "user_avatar": "https://remote/comment.jpg"}, "now"
    )])
    for owner_type, owner_id, name in (
        ("user", "10001", "main.png"), ("retweeted_user", "200", "retweet.png"), ("comment", "300", "comment.png")
    ):
        path = root / "media" / "avatars" / name
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"avatar")
        repo.upsert_media(MediaRecord(owner_type, owner_id, "avatar", 0, "https://remote.invalid/avatar", f"media/avatars/{name}"))
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    data = (root / "data" / "archive-data.js").read_text(encoding="utf-8")

    for path in ("media/avatars/main.png", "media/avatars/retweet.png", "media/avatars/comment.png"):
        assert path in data
    assert "remote/avatar" not in data and "remote/comment" not in data


def test_rollback_failure_keeps_durable_journal_and_backups(tmp_path, monkeypatch):
    import weibo_book.archive.render_snapshot as module
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    for target in (root/"微博书.html",root/"微博书.pdf",root/"微博书.md",root/"data"/"archive-data.js"):
        target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(b"old")
    real = module.os.replace; published = 0
    def fail_publish(source, destination, *args, **kwargs):
        nonlocal published
        source_path=Path(source)
        if ".render-stage-" in source_path.as_posix() and Path(destination).name in {"微博书.html","微博书.pdf","微博书.md","archive-data.js"}:
            published += 1
            if published == 2: raise OSError("publish-failed")
        return real(source,destination,*args,**kwargs)
    monkeypatch.setattr(module.os,"replace",fail_publish)
    monkeypatch.setattr(ArchiveRenderer,"_restore_backup",classmethod(
        lambda cls, backup, target: (_ for _ in ()).throw(OSError("rollback-failed"))
    ))
    with pytest.raises(OSError,match="rollback-failed"):
        ArchiveRenderer(repo).render_all(root,render_pdf=lambda _html,pdf:pdf.write_bytes(b"pdf"))
    assert (root / "data" / ".weishushu-render-state.json").is_file()
    stages = list((root / "data").glob(".render-stage-*"))
    assert len(stages) == 1
    assert len(list((stages[0] / "backup").iterdir())) == 4


def test_real_pdf_uses_local_video_poster_without_waiting_for_invalid_video_metadata(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path); repo.upsert_post(_post("A",""))
    media=root/"media"; media.mkdir()
    (media/"video.mp4").write_bytes(b"not-a-real-video")
    (media/"cover.png").write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082"))
    repo.upsert_media(MediaRecord("post","A","video",0,"remote","media/video.mp4"))
    repo.upsert_media(MediaRecord("post","A","video_cover",0,"remote","media/cover.png"))

    paths=ArchiveRenderer(repo).render_all(root)

    assert paths["pdf"].stat().st_size > 0


def test_real_pdf_does_not_load_invalid_video_without_poster(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    media = root / "media"
    media.mkdir()
    (media / "video.mp4").write_bytes(b"not-a-real-video")
    repo.upsert_media(
        MediaRecord("post", "A", "video", 0, "remote", "media/video.mp4")
    )

    paths = ArchiveRenderer(repo).render_all(root)

    assert paths["pdf"].stat().st_size > 0


def test_default_pdf_splits_large_archive_and_merges_one_file(tmp_path, monkeypatch):
    from pypdf import PdfReader, PdfWriter
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    html = tmp_path / "print.html"
    html.write_text("<html></html>", encoding="utf-8")
    output = tmp_path / "微博书.pdf"
    visited = []

    class FakePage:
        def on(self, *_args):
            return None

        def goto(self, url, **_kwargs):
            visited.append(url)

        def wait_for_function(self, *_args, **_kwargs):
            return None

        def evaluate(self, expression):
            if "archive.posts.length" in expression:
                return 120
            return None

        def pdf(self, *, path, **_kwargs):
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            with Path(path).open("wb") as stream:
                writer.write(stream)

        def close(self):
            return None

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeContext())

    ArchiveRenderer._default_pdf(html, output, tmp_path, 120)

    assert len(visited) == 3
    assert "printStart=0&printLimit=50" in visited[0]
    assert "printStart=50&printLimit=50" in visited[1]
    assert "printStart=100&printLimit=20" in visited[2]
    assert len(PdfReader(output).pages) == 3


def test_render_cancellation_during_thumbnail_generation_publishes_nothing(tmp_path):
    from PIL import Image
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.errors import OperationCancelled

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    media = root / "media"
    media.mkdir()
    for index in range(3):
        path = media / f"{index}.png"
        Image.new("RGB", (800, 600), "orange").save(path)
        repo.upsert_media(
            MediaRecord("post", "A", "image", index, "remote", f"media/{index}.png")
        )
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(OperationCancelled):
        ArchiveRenderer(repo).render_all(root, cancel_requested=cancelled)

    for target in (
        root / "微博书.html",
        root / "微博书.pdf",
        root / "微博书.md",
        root / "data" / "archive-data.js",
    ):
        assert not target.exists()
    assert not list((root / "data").glob(".render-stage-*"))
    assert not (root / "data" / ".weishushu-render-state.json").exists()


def test_pdf_thumbnail_rejects_media_symlink_outside_archive(tmp_path):
    from PIL import Image
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.archive.repository import ArchiveError

    require_symlink_capability(target_is_directory=False)
    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(outside)
    media = root / "media"
    media.mkdir()
    link = media / "escape.png"
    link.symlink_to(outside)
    repo.upsert_media(
        MediaRecord("post", "A", "image", 0, "remote", "media/escape.png")
    )

    with pytest.raises(ArchiveError, match="符号链接"):
        ArchiveRenderer(repo).render_all(
            root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
        )


def test_pdf_thumbnail_parent_swap_cannot_escape_open_directory(tmp_path, monkeypatch):
    from weibo_book.archive.render_snapshot import (
        _SUPPORTS_DIRECTORY_FDS,
        UnsafeMediaPathError,
        _open_archive_media,
    )

    require_symlink_capability(target_is_directory=True)
    root = tmp_path / "archive"
    inside_parent = root / "media" / "posts"
    inside_parent.mkdir(parents=True)
    (inside_parent / "target.bin").write_bytes(b"inside")
    outside_parent = tmp_path / "outside" / "posts"
    outside_parent.mkdir(parents=True)
    (outside_parent / "target.bin").write_bytes(b"outside")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "target.bin" and not swapped:
            swapped = True
            inside_parent.rename(root / "media" / "original-posts")
            (root / "media" / "posts").symlink_to(
                outside_parent, target_is_directory=True
            )
        if dir_fd is None:
            # Windows 的 os.open 不接受 dir_fd 参数；nt 分支按完整路径打开。
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        "weibo_book.archive.render_snapshot.os.open", swapping_open
    )

    if _SUPPORTS_DIRECTORY_FDS:
        with _open_archive_media(root, "media/posts/target.bin") as stream:
            assert stream.read() == b"inside"
    else:
        with pytest.raises(UnsafeMediaPathError, match="打开时已变化"):
            with _open_archive_media(root, "media/posts/target.bin"):
                pass


def test_pdf_thumbnail_parent_swap_detected_on_windows_path_branch(
    tmp_path, monkeypatch
):
    """强制 os.name="nt" 分支：完整路径打开后逐级复核父目录身份。"""
    require_symlink_capability(target_is_directory=True)
    monkeypatch.setattr("weibo_book.archive.render_snapshot.os.name", "nt")

    from weibo_book.archive.render_snapshot import (
        UnsafeMediaPathError,
        _open_archive_media,
    )

    root = tmp_path / "archive"
    inside_parent = root / "media" / "posts"
    inside_parent.mkdir(parents=True)
    (inside_parent / "target.bin").write_bytes(b"inside")
    outside_parent = tmp_path / "outside" / "posts"
    outside_parent.mkdir(parents=True)
    (outside_parent / "target.bin").write_bytes(b"outside")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "target.bin" and not swapped:
            swapped = True
            inside_parent.rename(root / "media" / "original-posts")
            (root / "media" / "posts").symlink_to(
                outside_parent, target_is_directory=True
            )
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        "weibo_book.archive.render_snapshot.os.open", swapping_open
    )

    with pytest.raises(UnsafeMediaPathError, match="打开时已变化"):
        with _open_archive_media(root, "media/posts/target.bin"):
            pass


def test_cancel_after_last_fixed_output_publish_restores_previous_files(
    tmp_path, monkeypatch
):
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.errors import OperationCancelled

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    targets = {
        "html": root / "微博书.html",
        "pdf": root / "微博书.pdf",
        "markdown": root / "微博书.md",
        "data": root / "data" / "archive-data.js",
    }
    previous = {}
    for key, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        previous[key] = f"旧-{key}".encode("utf-8")
        target.write_bytes(previous[key])
    real_replace = os.replace
    cancelled = False

    def replace_and_cancel(source, destination, **kwargs):
        nonlocal cancelled
        result = real_replace(source, destination, **kwargs)
        if not kwargs and Path(destination) == targets["data"]:
            cancelled = True
        return result

    monkeypatch.setattr(
        "weibo_book.archive.render_snapshot.os.replace", replace_and_cancel
    )

    with pytest.raises(OperationCancelled):
        ArchiveRenderer(repo).render_all(
            root,
            render_pdf=lambda _html, pdf: pdf.write_bytes(b"new-pdf"),
            cancel_requested=lambda: cancelled,
        )

    assert {key: target.read_bytes() for key, target in targets.items()} == previous
    assert not list((root / "data").glob(".render-stage-*"))
    assert not (root / "data" / ".weishushu-render-state.json").exists()


def test_pause_after_pdf_render_restores_outputs_and_next_run_renders_again(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.errors import OperationPaused

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    targets = {
        "html": root / "微博书.html",
        "pdf": root / "微博书.pdf",
        "markdown": root / "微博书.md",
        "data": root / "data" / "archive-data.js",
    }
    previous: dict[str, bytes] = {}
    for key, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        previous[key] = f"旧-{key}".encode("utf-8")
        target.write_bytes(previous[key])

    paused = False
    rendered_stages: list[Path] = []

    def render_then_pause(_html: Path, pdf: Path) -> None:
        nonlocal paused
        rendered_stages.append(pdf.parent)
        pdf.write_bytes(b"paused-pdf")
        paused = True

    with pytest.raises(OperationPaused, match="任务已暂停"):
        ArchiveRenderer(repo).render_all(
            root,
            render_pdf=render_then_pause,
            pause_requested=lambda: paused,
        )

    assert {key: target.read_bytes() for key, target in targets.items()} == previous
    assert not list((root / "data").glob(".render-stage-*"))
    assert not (root / "data" / ".weishushu-render-state.json").exists()

    def render_again(_html: Path, pdf: Path) -> None:
        rendered_stages.append(pdf.parent)
        pdf.write_bytes(b"resumed-pdf")

    ArchiveRenderer(repo).render_all(
        root,
        render_pdf=render_again,
        pause_requested=lambda: False,
    )

    assert len(rendered_stages) == 2
    assert rendered_stages[0] != rendered_stages[1]
    assert targets["pdf"].read_bytes() == b"resumed-pdf"


def test_cancel_during_final_validation_is_linearized_before_commit(
    tmp_path, monkeypatch
):
    from backend.app.services.task_manager import TaskRecord
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.errors import OperationCancelled

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", ""))
    targets = {
        "html": root / "微博书.html",
        "pdf": root / "微博书.pdf",
        "markdown": root / "微博书.md",
        "data": root / "data" / "archive-data.js",
    }
    previous = {}
    for key, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        previous[key] = f"旧-{key}".encode("utf-8")
        target.write_bytes(previous[key])
    record = TaskRecord("render")
    original_validate = ArchiveRenderer._validate_render_paths
    validations = 0

    def validate_and_cancel(render_root, data_dir, paths):
        nonlocal validations
        original_validate(render_root, data_dir, paths)
        validations += 1
        if validations == 6:
            assert record.try_request_cancel()

    monkeypatch.setattr(
        ArchiveRenderer, "_validate_render_paths", staticmethod(validate_and_cancel)
    )

    with pytest.raises(OperationCancelled):
        ArchiveRenderer(repo).render_all(
            root,
            render_pdf=lambda _html, pdf: pdf.write_bytes(b"new-pdf"),
            cancel_requested=record._cancel_event.is_set,
            begin_commit=record.try_begin_commit,
        )

    assert {key: target.read_bytes() for key, target in targets.items()} == previous
    assert not record._commit_started


def test_markdown_links_images_video_live_and_html_anchors(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T01:00:00+00:00"))
    records = (
        ("image", 0, "media/posts/original.jpg"),
        ("image_thumbnail", 0, "media/posts/thumb.jpg"),
        ("video", 1, "media/posts/video.mp4"),
        ("video_cover", 1, "media/posts/video-cover.jpg"),
        ("live_photo", 2, "media/posts/live.mov"),
        ("live_photo_thumbnail", 2, "media/posts/live.jpg"),
    )
    for role, position, local_path in records:
        repo.upsert_media(MediaRecord("post", "BID1", role, position, "remote", local_path))

    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    assert "[![图片](media/posts/thumb.jpg)](media/posts/original.jpg)" in markdown
    assert "[播放视频](media/posts/video.mp4)" in markdown
    assert "[![实况照片](media/posts/live.jpg)](media/posts/live.mov)" in markdown
    assert "[在互动微博书中查看](微博书.html#post-BID1)" in markdown
    assert "在互动微博书中查看视频" not in markdown
    assert "在互动微博书中查看实况照片" not in markdown
    assert str(root) not in markdown


def test_markdown_is_readable_without_repeated_media_level_technical_links(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "BID1",
        "2026-07-14T16:38:01+08:00",
        text="正文 &quot;内容&quot;",
        link_card_payload={
            "title": "引用卡片",
            "description": "引用说明",
            "local_image": "media/cards/link.jpg",
            "url": "https://example.invalid/article",
        },
    ))
    repo.replace_current_comments("BID1", [
        CommentRecord("C1", "BID1", None, {"text": "评论", "user_name": "甲"}, "now")
    ])
    for owner_type, owner_id, role, position, local_path in (
        ("post", "BID1", "image", 0, "media/posts/image.jpg"),
        ("post", "BID1", "live_photo", 1, "media/posts/live.mov"),
        ("post", "BID1", "live_photo_thumbnail", 1, "media/posts/live.jpg"),
        ("comment", "C1", "image", 0, "media/comments/comment.jpg"),
    ):
        repo.upsert_media(MediaRecord(
            owner_type, owner_id, role, position, "remote", local_path
        ))

    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    assert "2026-07-14 16:38" in markdown
    assert "T16:38:01+08:00" not in markdown
    assert "&quot;" not in markdown
    assert "&amp;quot;" not in markdown
    assert "在互动微博书中查看图片" not in markdown
    assert "在互动微博书中查看引用卡片" not in markdown
    assert "在互动微博书中查看评论图片" not in markdown
    assert "播放实况照片视频" in markdown
    assert markdown.count("[在互动微博书中查看]") == 1


def test_pdf_source_has_relative_media_links_without_local_paths(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T01:00:00+00:00"))
    records = (
        ("image", 0, "media/posts/original.jpg"),
        ("image_thumbnail", 0, "media/posts/thumb.jpg"),
        ("video", 1, "media/posts/video.mp4"),
        ("video_cover", 1, "media/posts/video-cover.jpg"),
        ("live_photo", 2, "media/posts/live.mov"),
        ("live_photo_thumbnail", 2, "media/posts/live.jpg"),
    )
    for role, position, local_path in records:
        repo.upsert_media(MediaRecord("post", "BID1", role, position, "remote", local_path))
    seen = {}

    def inspect_source(print_html, pdf):
        seen["source"] = print_html.read_text(encoding="utf-8")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            page.emulate_media(media="print")
            seen["post_id"] = page.locator('[id="post-BID1"]').count()
            seen["image_href"] = page.locator(
                '[data-media-owner="main:0"] .pdf-media-link'
            ).get_attribute("href")
            seen["video_href"] = page.locator(
                '[data-media-owner="main:1"] .pdf-media-link'
            ).get_attribute("href")
            seen["live_href"] = page.locator(
                '[data-media-owner="main:2"] .pdf-media-link'
            ).get_attribute("href")
            seen["print_text"] = page.locator("body").inner_text()
            seen["absolute_links"] = page.locator('a[href^="file:"]').count()
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect_source)
    source = seen["source"]

    assert seen["post_id"] == 1
    assert seen["image_href"] == "微博书.html#post-BID1&media=main:0"
    assert seen["video_href"] == "微博书.html#post-BID1&media=main:1"
    assert seen["live_href"] == "微博书.html#post-BID1&media=main:2"
    assert "本地文件：" not in seen["print_text"]
    assert seen["absolute_links"] == 0
    for output in (
        root / "微博书.html",
        root / "微博书.md",
        root / "data" / "archive-data.js",
    ):
        assert str(root) not in output.read_text(encoding="utf-8")


def test_moved_archive_html_media_anchor_opens_the_requested_local_item(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T01:00:00+00:00"))
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082"
    )
    image = root / "media" / "posts" / "original.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(png)
    repo.upsert_media(MediaRecord("post", "BID1", "image", 0, "remote", "media/posts/original.png"))
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )
    repo.close()
    moved = tmp_path / "moved" / "archive"
    moved.parent.mkdir()
    root.rename(moved)

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(
            (moved / "微博书.html").resolve().as_uri() + "#post-BID1&media=0",
            wait_until="load",
        )
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        assert page.locator('[data-view="detail"]').is_visible()
        assert page.locator("[data-media-lightbox]").is_visible()
        assert page.locator("[data-lightbox-image]").get_attribute("src") == "media/posts/original.png"
        assert page.locator("[data-lightbox-image]").evaluate("image => image.naturalWidth") == 1
        browser.close()


def test_real_pdf_has_portable_links_image_appendix_and_return_text(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    pdfinfo = shutil.which("pdfinfo")
    pdftotext = shutil.which("pdftotext")
    if pdfinfo and not pdftotext:
        dependency_root = Path(pdfinfo).resolve().parents[2]
        for bundled in (
            dependency_root / "native" / "poppler" / "bin" / "pdftotext",
            dependency_root / "native" / "poppler" / "poppler" / "bin" / "pdftotext",
        ):
            if bundled.is_file():
                pdftotext = str(bundled)
                break
    if not pdfinfo or not pdftotext:
        pytest.skip("本机未安装 pdfinfo/pdftotext")

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T01:00:00+00:00", text="PDF 正文"))
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082"
    )
    files = {
        "media/posts/original.png": png,
        "media/posts/thumb.png": png,
        "media/posts/video.mp4": b"local-video",
        "media/posts/video-cover.png": png,
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for role, position, relative in (
        ("image", 0, "media/posts/original.png"),
        ("image_thumbnail", 0, "media/posts/thumb.png"),
        ("video", 1, "media/posts/video.mp4"),
        ("video_cover", 1, "media/posts/video-cover.png"),
    ):
        repo.upsert_media(MediaRecord("post", "BID1", role, position, "remote", relative))

    ArchiveRenderer(repo).render_all(root)
    repo.close()
    moved = tmp_path / "moved-parent" / "archive"
    moved.parent.mkdir()
    root.rename(moved)
    pdf = moved / "微博书.pdf"
    info = subprocess.run(
        [pdfinfo, str(pdf)], check=True, capture_output=True, text=True
    ).stdout
    urls = subprocess.run(
        [pdfinfo, "-url", str(pdf)], check=True, capture_output=True, text=True
    ).stdout
    destinations = subprocess.run(
        [pdfinfo, "-dests", str(pdf)], check=True, capture_output=True, text=True
    ).stdout
    text = subprocess.run(
        [pdftotext, str(pdf), "-"], check=True, capture_output=True, text=True
    ).stdout

    pages = next(
        int(line.split(":", 1)[1].strip())
        for line in info.splitlines()
        if line.startswith("Pages:")
    )
    assert pages >= 1
    assert "PDF 正文" in text
    assert "file:" not in urls
    assert "%E5%BE%AE%E5%8D%9A%E4%B9%A6.html" in urls
    assert str(root) not in urls and str(moved) not in urls
    assert "post-BID1" in urls

    from urllib.parse import unquote, urljoin, urlsplit
    relative_uri = next(
        line.split()[-1] for line in urls.splitlines()
        if "%E5%BE%AE%E5%8D%9A%E4%B9%A6.html" in line
    )
    resolved = urljoin(pdf.resolve().as_uri(), relative_uri)
    assert Path(unquote(urlsplit(resolved).path)) == moved / "微博书.html"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.goto(pdf.resolve().as_uri(), wait_until="commit")
        assert page.url == pdf.resolve().as_uri()
        browser.close()


def test_markdown_preserves_complete_weibo_hierarchy_and_escapes_user_markup(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    root = tmp_path / "archive"
    repo = ArchiveRepository.create(root, "10001", "博主 <img src=https://evil.invalid/avatar>")
    repo.upsert_post(_post(
        "BID1", "2026-07-14T01:00:00+00:00",
        text="正文 <img src=https://evil.invalid/post> [注入](https://evil.invalid)",
        source="iPhone", ip_location="发布于 韩国", is_pinned=True,
        reposts_count=1, comments_count=2, likes_count=3,
        link_card_payload={"title": "主引用", "description": "主描述", "url": "https://example.invalid/main", "local_image": "media/cards/main.png"},
        retweeted_payload={
            "bid": "RBID", "uid": "200", "user_name": "转发作者", "text": "转发正文",
            "created_at": "2026-07-13T01:00:00+00:00", "source": "Android", "ip_location": "发布于 北京",
            "media": [{"type": "image", "position": 0, "local_path": "media/retweet.png"}],
            "link_card": {"title": "转发引用", "local_image": "media/cards/retweet.png"},
        },
    ))
    repo.replace_current_comments("BID1", [
        CommentRecord("C1", "BID1", None, {"text": "一级评论", "user_name": "甲", "created_at": "2026-07-14 02:00", "source": "iPhone", "user_id": "300", "like_counts": 5, "is_blogger": True}, "now"),
        CommentRecord("C2", "BID1", "C1", {"text": "关联回复", "user_name": "乙", "created_at": "2026-07-14 02:01", "source": "Android", "reply_to_name": "甲", "user_id": "301"}, "now"),
    ])
    for owner_type, owner_id, role, position, path in (
        ("user", "10001", "avatar", 0, "media/avatars/main.png"),
        ("retweeted_user", "200", "avatar", 0, "media/avatars/retweet.png"),
        ("comment", "300", "avatar", 0, "media/avatars/comment-300.png"),
        ("comment", "301", "avatar", 0, "media/avatars/comment-301.png"),
        ("post", "BID1", "image", 0, "media/main.png"),
        ("comment", "C1", "image", 0, "media/comment.png"),
    ):
        repo.upsert_media(MediaRecord(owner_type, owner_id, role, position, "remote", path))

    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    for text in (
        "博主", "置顶", "2026-07-14 01:00", "iPhone", "发布于 韩国",
        "转发 1 · 评论 2 · 赞 3", "转发作者", "转发正文", "Android", "发布于 北京",
        "主引用", "主描述", "转发引用", "一级评论", "关联回复", "回复 @甲",
        "media/avatars/main.png", "media/avatars/retweet.png", "media/avatars/comment-300.png",
        "media/avatars/comment-301.png", "博主", "赞 5", "media/retweet.png", "media/comment.png",
        "微博书.html#post-BID1",
    ):
        assert text in markdown
    assert "  - **乙**" in markdown
    assert "<img" not in markdown
    assert "&lt;img src=https://evil.invalid/post&gt;" in markdown
    assert "[注入](https://evil.invalid)" not in markdown


def test_pdf_prints_comments_and_local_media_with_unique_owners(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "BID1", "2026-07-14T01:00:00+00:00",
        link_card_payload={"title": "主引用", "local_image": "media/cards/main.png"},
        retweeted_payload={
            "bid": "RBID", "uid": "200", "user_name": "转发作者", "text": "转发正文",
            "media": [{"type": "image", "position": 0, "local_path": "media/retweet.png"}],
            "link_card": {"title": "转发引用", "local_image": "media/cards/retweet.png"},
        },
    ))
    repo.replace_current_comments("BID1", [
        CommentRecord("C1", "BID1", None, {"text": "一级评论", "user_name": "甲", "user_id": "300", "like_counts": 5, "is_blogger": True}, "now"),
        CommentRecord("C2", "BID1", "C1", {"text": "关联回复", "user_name": "乙", "user_id": "301", "reply_to_name": "甲", "like_counts": 7}, "now"),
    ])
    repo.upsert_media(MediaRecord("post", "BID1", "image", 0, "remote", "media/main.png"))
    repo.upsert_media(MediaRecord("comment", "C1", "image", 0, "remote", "media/comment.png"))
    seen = {}

    def inspect(print_html, pdf):
        source = print_html.read_text(encoding="utf-8")
        seen["source"] = source
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            seen["text"] = page.locator("body").inner_text()
            seen["owners"] = page.locator("[data-media-owner]").evaluate_all(
                "nodes => nodes.map(node => node.dataset.mediaOwner)"
            )
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.locator('[data-bid="BID1"] > .wb-post-header').click()
        seen["html_text"] = page.locator('[data-view="detail"] .comments').inner_text()
        browser.close()

    assert "一级评论" in seen["text"] and "关联回复" in seen["text"]
    for rendered in (seen["text"], seen["html_text"]):
        assert "博主" in rendered
        assert "回复 @甲" in rendered
        assert "赞 5" in rendered and "赞 7" in rendered
    assert set(seen["owners"]) >= {"main:0", "retweet:0", "comment:C1:0", "link:0", "retweet-link:0"}
    assert ".pdf-appendices" not in seen["source"]


@pytest.mark.parametrize(
    ("media_owner", "expected"),
    (("main:0", "media/main.png"), ("retweet:0", "media/retweet.png"), ("comment:C1:0", "media/comment.png")),
)
def test_moved_archive_namespaced_media_deep_link_focuses_exact_owner(tmp_path, media_owner, expected):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "", retweeted_payload={
        "bid": "RBID", "uid": "200", "user_name": "转发作者", "text": "转发",
        "media": [{"type": "image", "position": 0, "local_path": "media/retweet.png"}],
    }))
    repo.replace_current_comments("BID1", [CommentRecord("C1", "BID1", None, {"text": "评论", "user_name": "甲"}, "now")])
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63606060f80f0001040100b51c0c020000000049454e44ae426082")
    for owner_type, owner_id, path in (("post", "BID1", "media/main.png"), ("comment", "C1", "media/comment.png")):
        file_path = root / path; file_path.parent.mkdir(exist_ok=True); file_path.write_bytes(png)
        repo.upsert_media(MediaRecord(owner_type, owner_id, "image", 0, "remote", path))
    (root / "media" / "retweet.png").write_bytes(png)
    ArchiveRenderer(repo).render_all(root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"))
    repo.close(); moved = tmp_path / "moved"; root.rename(moved)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True); page = browser.new_page()
        page.goto((moved / "微博书.html").resolve().as_uri() + f"#post-BID1&media={media_owner}", wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        assert page.locator("[data-media-lightbox]").is_visible()
        assert page.locator("[data-lightbox-image]").get_attribute("src") == expected
        browser.close()


@pytest.mark.parametrize("viewport_width", [375, 390, 680, 1280])
def test_interactive_link_cards_are_separate_local_safe_components(tmp_path, viewport_width):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "BID1",
        "2026-07-14T01:00:00+00:00",
        link_card_payload={
            "title": "主微博引用标题",
            "description": "这是一段较长的引用说明，用于验证窄屏下文本不会撑破卡片。",
            "url": "https://example.invalid/main-card",
            "local_image": "media/cards/main.png",
        },
        retweeted_payload={
            "bid": "RBID",
            "uid": "200",
            "user_name": "转发作者",
            "text": "转发正文",
            "link_card": {
                "title": "转发引用标题",
                "description": "转发引用说明",
            },
        },
    ))
    for relative in (
        "media/posts/normal.png",
        "media/cards/main.png",
    ):
        _write_local_png(root, relative)
    repo.upsert_media(MediaRecord(
        "post", "BID1", "image", 0, "remote", "media/posts/normal.png"
    ))
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport_width, "height": 844})
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        cards = page.locator('[data-bid="BID1"] .wb-link-card')
        article = page.locator('article[data-bid="BID1"]')
        assert article.get_attribute("role") is None
        assert article.get_attribute("tabindex") is None
        detail_button = article.locator('[data-open-post="BID1"]')
        assert detail_button.count() == 1
        assert detail_button.evaluate("node => node.tagName") == "BUTTON"
        assert detail_button.get_attribute("type") == "button"
        assert detail_button.get_attribute("aria-label") == "打开微博正文 BID1"
        assert cards.count() == 2
        assert cards.locator(".wb-link-card__image").count() == 1
        assert cards.locator(".wb-link-card__content").count() == 2
        assert cards.locator(".wb-link-card__title").count() == 2
        assert cards.locator(".wb-link-card__description").count() == 2
        assert cards.locator("[data-local-image]").count() == 0
        assert cards.locator("[data-media-group]").count() == 0
        assert page.locator('[data-bid="BID1"] .media-grid [data-local-image]').count() == 1

        linked = cards.filter(has=page.locator(".wb-link-card__title", has_text="主微博引用标题"))
        assert linked.evaluate("node => node.tagName") == "A"
        assert linked.get_attribute("target") == "_blank"
        assert linked.get_attribute("rel") == "noopener noreferrer"
        unlinked = cards.filter(has=page.locator(".wb-link-card__title", has_text="转发引用标题"))
        assert unlinked.evaluate("node => node.tagName") == "DIV"
        assert unlinked.locator("a").count() == 0
        unlinked_widths = unlinked.evaluate("""node => ({
            card: node.getBoundingClientRect().width,
            content: node.querySelector('.wb-link-card__content').getBoundingClientRect().width
        })""")
        assert unlinked_widths["content"] == unlinked_widths["card"]

        linked.evaluate("node => node.addEventListener('click', event => event.preventDefault(), {once:true})")
        linked.click()
        assert page.locator('[data-view="detail"]').is_hidden()
        assert not any(url.startswith("https://example.invalid/") for url in requests)

        linked.evaluate("node => node.addEventListener('click', event => event.preventDefault(), {once:true})")
        linked.focus()
        page.keyboard.press("Enter")
        assert page.locator('[data-view="detail"]').is_hidden()

        detail_button.focus()
        page.keyboard.press("Enter")
        assert page.locator('[data-view="detail"]').is_visible()
        assert page.locator('[data-view="detail"] [data-open-post]').count() == 0
        page.locator('[data-action="back"]').click()
        assert page.locator('[data-view="detail"]').is_hidden()

        metrics = cards.evaluate_all("""nodes => ({
            viewport: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            rightEdges: nodes.map(node => node.getBoundingClientRect().right),
            cardWidths: nodes.map(node => node.getBoundingClientRect().width)
        })""")
        assert metrics["documentWidth"] <= metrics["viewport"]
        assert all(edge <= metrics["viewport"] for edge in metrics["rightEdges"])
        assert all(width > 0 for width in metrics["cardWidths"])
        browser.close()


def test_interactive_link_card_uses_exact_fallback_for_missing_or_empty_title(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post(
        "MISSING", "2026-07-14T02:00:00+00:00",
        link_card_payload={"description": "缺失标题说明"},
    ))
    repo.upsert_post(_post(
        "EMPTY", "2026-07-14T01:00:00+00:00",
        link_card_payload={"title": "", "description": "空标题说明"},
    ))
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        for bid, description in (
            ("MISSING", "缺失标题说明"),
            ("EMPTY", "空标题说明"),
        ):
            card = page.locator(f'[data-bid="{bid}"] .wb-link-card')
            assert card.locator(".wb-link-card__title").inner_text() == "引用卡片"
            assert card.locator(".wb-link-card__description").inner_text() == description
            card_text = card.inner_text()
            assert "阅读" not in card_text
            assert "讨论" not in card_text
            assert "播放" not in card_text
            assert "点赞" not in card_text
        browser.close()


def test_interactive_detail_limits_root_comments_and_keeps_current_comment_media(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("BID1", "2026-07-14T01:00:00+00:00", comments_count=13))
    comments = [
        CommentRecord(
            f"ROOT-{index}",
            "BID1",
            None,
            {
                "text": f"一级评论 {index}",
                "user_name": f"用户 {index}",
                "user_id": f"USER-{index}",
                "created_at": f"2026-07-14 02:{index:02d}",
                "source": "iPhone",
                "like_counts": index,
            },
            "now",
        )
        for index in range(1, 12)
    ]
    comments.insert(1, CommentRecord(
        "REPLY-1", "BID1", "ROOT-1",
        {"text": "第一条的关联回复", "user_name": "回复者甲", "reply_to_name": "用户 1"},
        "now",
    ))
    comments.append(CommentRecord(
        "REPLY-11", "BID1", "ROOT-11",
        {"text": "第十一条的关联回复", "user_name": "回复者乙"},
        "now",
    ))
    repo.replace_current_comments("BID1", comments)
    _write_local_png(root, "media/comments/root-1.png")
    _write_local_png(root, "media/avatars/user-1.png")
    repo.upsert_media(MediaRecord(
        "comment", "ROOT-1", "image", 0, "remote", "media/comments/root-1.png"
    ))
    repo.upsert_media(MediaRecord(
        "comment", "USER-1", "avatar", 0, "remote", "media/avatars/user-1.png"
    ))
    repo.apply_post_change(_post(
        "BID1", "2026-07-14T01:00:00+00:00", text="当前正文", comments_count=13
    ))
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        print_page = browser.new_page(viewport={"width": 390, "height": 844})
        print_page.goto(
            (root / "微博书.html").resolve().as_uri() + "?print=1",
            wait_until="load",
        )
        print_page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        print_feed_card = print_page.locator('[data-bid="BID1"]')
        assert print_feed_card.locator(".comments").count() == 0
        assert "评论 13" in print_feed_card.locator(".wb-post-actions").inner_text()
        print_page.close()

        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        feed_card = page.locator('[data-bid="BID1"]')
        assert feed_card.locator(".comments").count() == 0
        assert "评论 13" in feed_card.locator(".wb-post-actions").inner_text()
        feed_card.locator("> .wb-post-header").click()

        detail_comments = page.locator('[data-view="detail"] .comments')
        visible_text = detail_comments.inner_text()
        assert detail_comments.locator(".comment:not(.reply)").count() == 10
        assert detail_comments.locator(".comment.reply").count() == 1
        assert "一级评论 10" in visible_text
        assert "第一条的关联回复" in visible_text
        assert "一级评论 11" not in visible_text
        assert "第十一条的关联回复" not in visible_text
        assert "2026-07-14 02:01" in visible_text
        assert "iPhone" in visible_text
        assert "赞 1" in visible_text
        assert detail_comments.locator('.comment-avatar[src="media/avatars/user-1.png"]').count() == 1

        comment_image_button = detail_comments.locator(
            '[data-local-image="media/comments/root-1.png"]'
        )
        assert comment_image_button.count() == 1
        assert comment_image_button.evaluate("node => node.tagName") == "BUTTON"
        assert comment_image_button.get_attribute("type") == "button"
        assert comment_image_button.get_attribute("aria-label") == "查看评论图片"
        assert comment_image_button.locator("img").get_attribute("tabindex") is None
        comment_image_button.click()
        assert page.locator("[data-media-lightbox]").is_visible()
        assert page.locator("[data-lightbox-image]").get_attribute("src") == "media/comments/root-1.png"
        page.locator('[data-action="lightbox-close"]').click()
        assert comment_image_button.evaluate("node => document.activeElement === node") is True

        comment_image_button.focus()
        page.keyboard.press("Space")
        assert page.locator("[data-media-lightbox]").is_visible()
        page.locator('[data-action="lightbox-close"]').click()
        assert comment_image_button.evaluate("node => document.activeElement === node") is True

        page.locator("[data-revision-index]").click()
        revised_text = page.locator('[data-view="detail"]').inner_text()
        assert "正文 BID1" in revised_text
        assert "第一条的关联回复" in revised_text
        assert "一级评论 11" not in revised_text
        browser.close()


def test_archive_html_pdf_source_and_markdown_keep_original_blank_lines(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    text = "第一段第一行\n第一段第二行\n\n第二段"
    repository.upsert_post(_post("LINES", "2026-07-26T12:00:00+09:00", text=text))
    captured_print_html = ""

    def render_pdf(html_path, pdf_path):
        nonlocal captured_print_html
        captured_print_html = html_path.read_text(encoding="utf-8")
        pdf_path.write_bytes(b"pdf")

    ArchiveRenderer(repository).render_all(root, render_pdf=render_pdf)

    markdown = (root / "微博书.md").read_text(encoding="utf-8")
    data = (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    assert text in markdown
    assert "第一段第一行\\n第一段第二行\\n\\n第二段" in data
    assert "white-space:pre-wrap" in captured_print_html


def test_markdown_has_book_toc_year_month_hierarchy_and_visibility_badge(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("PIN", "", is_pinned=True, pin_order=1))
    repo.upsert_post(_post("AUG", "2026-08-20T10:00:00+00:00"))
    repo.upsert_post(_post("JUL-2", "2026-07-20T10:00:00+00:00"))
    repo.upsert_post(_post(
        "JUL-1", "2026-07-05T10:00:00+00:00", visibility="unavailable"
    ))
    repo.upsert_post(_post("OLD", "2025-12-01T10:00:00+00:00"))

    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    assert markdown.startswith("# 固定名字 的微博书\n")
    assert "- 微博 UID：10001" in markdown
    assert "- 收录微博：5 条" in markdown
    assert "- 时间跨度：2025-12-01 10:00 至 2026-08-20 10:00" in markdown
    assert markdown.index("## 目录") < markdown.index("## 2026 年")
    assert "- 2026 年 8 月（1 条）" in markdown
    assert "- 2026 年 7 月（2 条）" in markdown
    assert "- 2025 年 12 月（1 条）" in markdown
    assert "\n## 2026 年\n" in markdown
    assert "\n### 2026 年 8 月\n" in markdown
    assert "\n### 2026 年 7 月\n" in markdown
    assert "\n## 2025 年\n" in markdown
    assert "\n### 2025 年 12 月\n" in markdown
    assert "#### 固定名字 · 置顶" in markdown
    assert "#### 固定名字 · 不可见" in markdown
    assert "\n#### 固定名字\n" in markdown
    assert markdown.index("### 2026 年 8 月") < markdown.index("### 2026 年 7 月")
    assert markdown.index("## 2025 年") > markdown.index("### 2026 年 7 月")


def test_markdown_omits_toc_for_archive_without_dated_posts(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("NODATE", ""))

    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    assert "## 目录" not in markdown
    assert "#### 固定名字" in markdown


def test_print_first_batch_renders_cover_toc_and_chapter_anchors(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("PIN", "", is_pinned=True, pin_order=1))
    repo.upsert_post(_post("AUG", "2026-08-20T10:00:00+00:00"))
    repo.upsert_post(_post("JUL-2", "2026-07-20T10:00:00+00:00"))
    repo.upsert_post(_post("JUL-1", "2026-07-05T10:00:00+00:00"))
    repo.upsert_post(_post("OLD", "2025-12-01T10:00:00+00:00"))
    observed = {}

    def inspect(print_html, pdf):
        observed["source"] = print_html.read_text(encoding="utf-8")
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                print_html.resolve().as_uri() + "?print=1&printStart=0",
                wait_until="load",
            )
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            page.emulate_media(media="print")
            observed["cover"] = page.locator(".pdf-cover").inner_text()
            observed["toc"] = page.locator(".pdf-toc").inner_text()
            observed["toc_hrefs"] = page.locator(".pdf-toc a").evaluate_all(
                "links => links.map(link => link.getAttribute('href'))"
            )
            observed["year_ids"] = page.locator(".pdf-chapter-year").evaluate_all(
                "nodes => nodes.map(node => node.id)"
            )
            observed["month_ids"] = page.locator(".pdf-chapter-month").evaluate_all(
                "nodes => nodes.map(node => node.id)"
            )
            observed["year_break"] = page.locator(".pdf-chapter-year").first.evaluate(
                "node => getComputedStyle(node).breakBefore"
            )
            observed["cards"] = page.locator(".feed-card").count()
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert "固定名字" in observed["cover"]
    assert "共 5 条微博" in observed["cover"]
    assert "2025-12-01 10:00" in observed["cover"]
    assert "2026-08-20 10:00" in observed["cover"]
    assert "生成时间" in observed["cover"]
    assert "目录" in observed["toc"]
    assert "2026 年 8 月（1 条）" in observed["toc"]
    assert "2025 年 12 月（1 条）" in observed["toc"]
    assert observed["toc_hrefs"] == [
        "#chapter-2026-08", "#chapter-2026-07", "#chapter-2025-12"
    ]
    assert observed["year_ids"] == ["chapter-2026", "chapter-2025"]
    assert observed["month_ids"] == [
        "chapter-2026-08", "chapter-2026-07", "chapter-2025-12"
    ]
    assert observed["year_break"] == "page"
    assert observed["cards"] == 5
    assert "@page" in observed["source"]
    assert "A4" in observed["source"]


def test_print_later_batch_omits_cover_and_toc(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    for index in range(3):
        repo.upsert_post(_post(f"P{index}", f"2026-07-1{index}T10:00:00+00:00"))
    observed = {}

    def inspect(print_html, pdf):
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                print_html.resolve().as_uri() + "?print=1&printStart=1&printLimit=2",
                wait_until="load",
            )
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            observed["cover"] = page.locator(".pdf-cover").count()
            observed["toc"] = page.locator(".pdf-toc").count()
            observed["cards"] = page.locator(".feed-card").count()
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert observed["cover"] == 0
    assert observed["toc"] == 0
    assert observed["cards"] == 2


@pytest.mark.parametrize(
    ("image_count", "expected_columns"),
    [(1, 1), (2, 2), (3, 3), (4, 2), (9, 3)],
)
def test_print_media_grid_book_layout(tmp_path, image_count, expected_columns):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    for position in range(image_count):
        repo.upsert_media(
            MediaRecord("post", "A", "image", position, "remote", "media/one.png")
        )
    observed = {}

    def inspect(print_html, pdf):
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                pytest.skip("本机未安装 Playwright Chromium")
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(print_html.resolve().as_uri() + "?print=1", wait_until="load")
            page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
            page.emulate_media(media="print")
            grid = page.locator(".feed-card .media-grid")
            observed["count"] = grid.get_attribute("data-count")
            observed["columns"] = grid.evaluate(
                "grid => getComputedStyle(grid).gridTemplateColumns.split(' ').length"
            )
            observed["child_ratio"] = grid.locator(":scope > *").first.evaluate(
                "node => getComputedStyle(node).aspectRatio"
            )
            observed["image_fit"] = page.locator(
                ".pdf-media-link img"
            ).first.evaluate("image => getComputedStyle(image).objectFit")
            browser.close()
        pdf.write_bytes(b"pdf")

    ArchiveRenderer(repo).render_all(root, render_pdf=inspect)

    assert observed["count"] == str(image_count)
    assert observed["columns"] == expected_columns
    if image_count == 1:
        assert observed["child_ratio"] == "auto"
        assert observed["image_fit"] == "contain"
    else:
        assert observed["child_ratio"] != "auto"
