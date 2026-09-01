from __future__ import annotations

import json
from pathlib import Path

import pytest

from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord


def _repo(tmp_path: Path) -> tuple[ArchiveRepository, Path]:
    root = tmp_path / "archive"
    return ArchiveRepository.create(root, "10001", "固定名字"), root


def _post(bid: str, created_at: str, **changes) -> PostRecord:
    values = {
        "bid": bid,
        "uid": "10001",
        "text": f"正文 {bid}",
        "created_at": created_at,
    }
    values.update(changes)
    return PostRecord(**values)


def test_snapshot_builds_exact_month_index_without_pins_or_invalid_times(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repository, _ = _repo(tmp_path)
    for post in (
        _post("PIN", "", is_pinned=True, pin_order=1),
        _post("JUL-2", "2026-07-18T09:30:00+09:00"),
        _post("JUL-1", "2026-07-01T08:00:00+09:00"),
        _post("MAR", "2026-03-15T12:00:00+09:00"),
        _post("OLD", "2024-12-31T23:59:00+09:00"),
        _post("BAD", "无法解析的时间"),
    ):
        repository.upsert_post(post)

    snapshot = ArchiveRenderSnapshot.from_repository(repository)

    assert snapshot.timeline == {
        "months": (
            {"key": "2026-07", "year": 2026, "month": 7, "start": 0, "end": 2},
            {"key": "2026-03", "year": 2026, "month": 3, "start": 2, "end": 3},
            {"key": "2024-12", "year": 2024, "month": 12, "start": 3, "end": 4},
        ),
        "normal_count": 5,
    }
    assert [post["bid"] for post in snapshot.posts] == [
        "PIN",
        "JUL-2",
        "JUL-1",
        "MAR",
        "OLD",
        "BAD",
    ]
    with pytest.raises(TypeError, match="只读"):
        snapshot.timeline["normal_count"] = 0


def test_archive_data_contains_timeline_without_changing_payload_schema(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    repository.upsert_post(_post("A", "2026-07-18T09:30:00+09:00"))
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    source = (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    payload = json.loads(
        source.removeprefix("window.__WEISHUSHU_ARCHIVE__ = ").removesuffix(";\n")
    )
    assert payload["schema"] == 1
    assert payload["timeline"]["months"] == [
        {"end": 1, "key": "2026-07", "month": 7, "start": 0, "year": 2026}
    ]


def test_large_archive_renders_bounded_window_and_keeps_pins_latest_only(tmp_path):
    from playwright.sync_api import sync_playwright

    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    repository.upsert_post(_post("PIN", "", is_pinned=True, pin_order=1))
    for index in range(960):
        year = 2026 - (index // 120)
        month = 12 - ((index // 10) % 12)
        day = (index % 10) + 1
        repository.upsert_post(
            _post(
                f"P{index:04d}",
                f"{year:04d}-{month:02d}-{day:02d}T08:00:00+09:00",
            )
        )
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        assert page.locator(".feed-card").count() == 61
        assert page.locator('[data-bid="PIN"]').count() == 1
        page.locator('[data-action="older-window"]').click()
        assert page.locator(".feed-card").count() == 60
        assert page.locator('[data-bid="PIN"]').count() == 0
        assert page.locator('[data-action="latest-window"]').is_visible()
        page.locator('[data-action="latest-window"]').click()
        assert page.locator('[data-bid="PIN"]').count() == 1
        page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2019-06')")
        assert page.locator("[data-timeline-directory]").evaluate(
            "node => node.getBoundingClientRect().top >= 0"
        ) is True
        assert page.locator("[data-timeline-target]").evaluate(
            "card => card.getBoundingClientRect().top >= 60"
        ) is True
        browser.close()


def test_desktop_time_directory_counts_expands_one_year_and_keeps_jump_rules(tmp_path):
    from playwright.sync_api import sync_playwright

    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    for bid, value in (
        ("JUL", "2026-07-18T09:00:00+09:00"),
        ("MAR", "2026-03-18T09:00:00+09:00"),
        ("OLD", "2024-12-18T09:00:00+09:00"),
    ):
        repository.upsert_post(_post(bid, value))
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        directory = page.locator("[data-timeline-directory]")
        assert directory.is_visible()
        assert page.locator('[data-timeline-range]').count() == 0
        assert page.locator('[data-timeline-handle]').count() == 0
        assert page.locator('[data-timeline-tip]').count() == 0

        years = page.locator("[data-timeline-year]")
        assert years.count() == 2
        assert years.nth(0).get_attribute("data-timeline-year") == "2026"
        assert years.nth(0).get_attribute("aria-expanded") == "true"
        assert years.nth(1).get_attribute("aria-expanded") == "false"
        assert years.nth(0).locator("[data-timeline-count]").inner_text() == "2"
        assert years.nth(1).locator("[data-timeline-count]").inner_text() == "1"

        months = page.locator("[data-timeline-month]")
        assert months.count() == 2
        assert months.nth(0).get_attribute("data-timeline-month") == "2026-07"
        assert months.nth(0).locator("[data-timeline-count]").inner_text() == "1"
        assert months.nth(1).get_attribute("data-timeline-month") == "2026-03"
        assert months.nth(1).locator("[data-timeline-count]").inner_text() == "1"

        years.nth(1).click()
        assert years.nth(0).get_attribute("aria-expanded") == "false"
        assert years.nth(1).get_attribute("aria-expanded") == "true"
        assert page.locator("[data-timeline-month]").count() == 1
        assert page.locator(
            '[data-timeline-month][aria-current="true"]'
        ).count() == 0

        assert (
            page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2026-05')")
            == "2026-07"
        )
        assert (
            page.locator('[data-bid="JUL"]').get_attribute("data-timeline-target")
            == "2026-05"
        )
        assert (
            page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2027-01')")
            == "2026-07"
        )
        assert page.locator("[data-timeline-directory]").evaluate(
            "node => node.getBoundingClientRect().top >= 0"
        ) is True
        browser.close()


def test_latest_window_restores_latest_month_selection(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    for bid, value in (
        ("LATEST", "2026-07-18T09:00:00+09:00"),
        ("OLD", "2024-12-18T09:00:00+09:00"),
    ):
        repository.upsert_post(_post(bid, value))
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 500})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2024-12')")
        page.locator('[data-action="latest-window"]').click()

        assert page.locator(
            '[data-timeline-month="2026-07"][aria-current="true"]'
        ).count() == 1
        browser.close()


def test_scroll_sync_uses_card_at_reading_anchor_not_last_visible_card(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    for bid, value in (
        ("JUL", "2026-07-18T09:00:00+09:00"),
        ("MAR", "2026-03-18T09:00:00+09:00"),
        ("OLD", "2024-12-18T09:00:00+09:00"),
    ):
        repository.upsert_post(_post(bid, value))
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 500})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        page.wait_for_timeout(100)
        page.add_style_tag(content=".feed-card{min-height:260px}")
        page.evaluate(
            """() => document.querySelector('[data-bid="JUL"]')
                .scrollIntoView({block:"start"})"""
        )
        page.wait_for_timeout(100)

        assert page.locator(
            '[data-timeline-month="2026-07"][aria-current="true"]'
        ).count() == 1
        browser.close()


def test_scrolling_feed_expands_and_highlights_visible_month_without_second_jump(tmp_path):
    from playwright.sync_api import sync_playwright
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    for bid, value in (
        ("JUL", "2026-07-18T09:00:00+09:00"),
        ("MAR", "2026-03-18T09:00:00+09:00"),
        ("OLD", "2024-12-18T09:00:00+09:00"),
    ):
        repository.upsert_post(_post(bid, value))
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 500})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
        page.evaluate(
            """() => {
                window.__timelineScrollIntoViewCount=0;
                const original=Element.prototype.scrollIntoView;
                Element.prototype.scrollIntoView=function(...args){
                    window.__timelineScrollIntoViewCount+=1;
                    return original.apply(this,args);
                };
            }"""
        )

        page.wait_for_timeout(100)
        page.evaluate(
            """() => {
                document.querySelector('[data-bid="OLD"]')
                    .scrollIntoView({block:"start"});
                window.__timelineScrollIntoViewCount=0;
            }"""
        )
        page.wait_for_function(
            """() => document.querySelector(
                '[data-timeline-month="2024-12"][aria-current="true"]'
            ) !== null"""
        )

        assert page.locator(
            '[data-timeline-year="2024"]'
        ).get_attribute("aria-expanded") == "true"
        assert page.evaluate("window.__timelineScrollIntoViewCount") == 0
        browser.close()


@pytest.mark.parametrize(
    ("width", "desktop_visible", "mobile_visible"),
    [
        (375, False, True),
        (390, False, True),
        (430, False, True),
        (680, False, True),
        (1280, True, False),
    ],
)
def test_time_navigation_uses_locked_responsive_forms(
    tmp_path, width, desktop_visible, mobile_visible
):
    from playwright.sync_api import sync_playwright

    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repository, root = _repo(tmp_path)
    for index in range(80):
        repository.upsert_post(
            _post(
                f"P{index:03d}",
                f"2026-{12-(index//10):02d}-{(index%10)+1:02d}T08:00:00+09:00",
            )
        )
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 844})
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        assert page.locator("[data-timeline-directory]").is_visible() is desktop_visible
        assert page.locator("[data-timeline-range]").count() == 0
        trigger = page.locator('[data-action="open-time-panel"]')
        assert trigger.is_visible() is mobile_visible
        if mobile_visible:
            page.evaluate("window.scrollTo(0, 300)")
            before = page.evaluate("window.scrollY")
            trigger.click()
            assert page.locator("[data-time-panel]").is_visible()
            page.locator('[data-action="close-time-panel"]').click()
            assert page.evaluate("window.scrollY") == before
            assert trigger.evaluate("node => document.activeElement === node") is True
        if desktop_visible:
            assert page.locator("[data-timeline-directory]").evaluate(
                """node => {
                    const rect=node.getBoundingClientRect();
                    return rect.width >= 160 &&
                           rect.right <= document.documentElement.clientWidth;
                }"""
            )
            assert page.evaluate(
                "document.documentElement.scrollWidth === document.documentElement.clientWidth"
            )
        browser.close()


def test_historical_window_keeps_detail_media_and_reading_position_offline(tmp_path):
    from playwright.sync_api import sync_playwright

    from weibo_book.archive.render_snapshot import ArchiveRenderer
    from weibo_book.archive.schema import CommentRecord, MediaRecord

    repository, root = _repo(tmp_path)
    for index in range(920):
        year = 2026 - (index // 120)
        month = 12 - ((index // 10) % 12)
        repository.upsert_post(
            _post(
                f"P{index:04d}",
                f"{year:04d}-{month:02d}-{(index%10)+1:02d}T08:00:00+09:00",
            )
        )
    target = "P0900"
    repository.replace_current_comments(
        target,
        [CommentRecord("C1", target, None, {"text": "早期评论"}, "now")],
    )
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001"
        "08060000001f15c4890000000d49444154789c63606060f80f"
        "0001040100b51c0c020000000049454e44ae426082"
    )
    for position, path in enumerate(("media/early-1.png", "media/early-2.png")):
        image = root / path
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(png)
        repository.upsert_media(
            MediaRecord("post", target, "image", position, "remote", path)
        )
    comment_image = root / "media/early-comment.png"
    comment_image.write_bytes(png)
    repository.upsert_media(
        MediaRecord("comment", "C1", "image", 0, "remote", "media/early-comment.png")
    )
    live_image = root / "media/early-live.png"
    live_video = root / "media/early-live.mov"
    live_image.write_bytes(png)
    live_video.write_bytes(b"local-live-video")
    repository.upsert_media(
        MediaRecord(
            "post",
            target,
            "live_photo_thumbnail",
            2,
            "remote",
            "media/early-live.png",
        )
    )
    repository.upsert_media(
        MediaRecord(
            "post", target, "live_photo", 2, "remote", "media/early-live.mov"
        )
    )
    ArchiveRenderer(repository).render_all(
        root, render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf")
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        requests: list[str] = []
        errors: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((root / "微博书.html").resolve().as_uri(), wait_until="load")
        page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")

        assert page.locator(f'[data-bid="{target}"]').count() == 0
        assert (
            page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2019-06')")
            == "2019-06"
        )
        feed_card = page.locator(f'[data-bid="{target}"]')
        feed_card.locator("[data-local-image]").first.click()
        assert page.locator("[data-media-lightbox]").is_visible()
        page.keyboard.press("Escape")
        live_photo = feed_card.locator("[data-live-photo]")
        live_video_node = live_photo.locator("video")
        live_video_node.evaluate(
            """video => {
                window.__timelinePauseCount=0;
                video.play=()=>Promise.resolve();
                video.pause=()=>{window.__timelinePauseCount+=1};
            }"""
        )
        live_photo.click()
        assert live_video_node.is_visible()
        live_photo.click()
        assert live_video_node.is_hidden()
        page.evaluate("window.__timelinePauseCount=0")
        live_photo.click()
        assert live_video_node.is_visible()
        page.locator('[data-action="newer-window"]').click()
        assert page.evaluate("window.__timelinePauseCount") == 1
        assert (
            page.evaluate("window.__WEISHUSHU_TIMELINE_TEST__.jump('2019-06')")
            == "2019-06"
        )
        page.locator(f'[data-bid="{target}"] .card-head').click()
        page.locator('[data-view="detail"] [data-local-image]').first.click()
        assert page.locator("[data-media-lightbox]").is_visible()
        page.keyboard.press("Escape")
        page.locator('[data-view="detail"] .comment-image-button').click()
        assert page.locator("[data-media-lightbox]").is_visible()
        assert page.locator("[data-lightbox-image]").get_attribute("src").endswith(
            "media/early-comment.png"
        )
        page.keyboard.press("Escape")
        page.go_back()
        assert page.locator('[data-view="feed"]').is_visible()
        assert page.locator(f'[data-bid="{target}"]').count() == 1
        assert page.locator(".feed-card").count() <= 60
        page.go_forward()
        assert page.locator('[data-view="detail"]').is_visible()
        page.go_back()
        assert page.locator('[data-view="feed"]').is_visible()
        page.locator('[data-action="latest-window"]').click()
        assert page.locator(f'[data-bid="{target}"]').count() == 0
        assert errors == []
        assert not any(url.startswith(("http://", "https://")) for url in requests)
        browser.close()
