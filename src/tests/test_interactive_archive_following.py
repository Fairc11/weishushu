from __future__ import annotations

from pathlib import Path

import pytest

from weibo_book.archive.following import FollowingObjectRecord
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord


def _repo(tmp_path: Path) -> tuple[ArchiveRepository, Path]:
    root = tmp_path / "archive"
    return ArchiveRepository.create(root, "10001", "测试用户"), root


def _post(bid: str, created_at: str, **changes) -> PostRecord:
    values = {"bid": bid, "uid": "10001", "text": f"正文 {bid}", "created_at": created_at}
    values.update(changes)
    return PostRecord(**values)


def _object(object_type: str = "blogger", object_id: str = "1000000001", **changes) -> FollowingObjectRecord:
    values = {
        "object_type": object_type,
        "object_id": object_id,
        "display_name": "测试博主" if object_type == "blogger" else "测试超话",
        "page_url": (
            f"https://weibo.com/u/{object_id}"
            if object_type == "blogger"
            else "//weibo.com/p/100808test"
        ),
        "app_scheme": (
            ""
            if object_type == "blogger"
            else "sinaweibo://pageinfo?containerid=100808test"
        ),
        "source_order": 0,
    }
    values.update(changes)
    return FollowingObjectRecord(**values)


def _commit(
    repository: ArchiveRepository,
    items: list[FollowingObjectRecord],
    cutoff: str = "2026-07-18T00:00:00+00:00",
):
    snapshot_id = repository.begin_following_snapshot(
        started_at="2026-07-17T23:00:00+00:00"
    )
    repository.stage_following_items(snapshot_id, items)
    return repository.commit_following_snapshot(
        snapshot_id,
        cutoff_at=cutoff,
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=sum(item.object_type == "blogger" for item in items),
        supertopic_reported_total=sum(
            item.object_type == "supertopic" for item in items
        ),
    )


def test_following_payload_projects_snapshot_items_relationships_names_changes(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    items = [
        _object("blogger", "1000000001", display_name="博主甲", source_order=0),
        _object("blogger", "1000000002", display_name="博主乙", source_order=1),
        _object("supertopic", "1022:100808test", display_name="测试超话", source_order=0),
    ]
    _commit(repo, items, cutoff="2026-07-18T00:00:00+00:00")

    following = ArchiveRenderSnapshot.from_repository(repo).payload()["following"]

    assert following is not None
    assert following["snapshot"]["cutoff_at"] == "2026-07-18T00:00:00+00:00"
    assert following["snapshot"]["status"] == "complete"
    assert following["snapshot"]["bloggers_complete"] is True
    assert following["snapshot"]["supertopics_complete"] is True
    assert following["snapshot"]["blogger_count"] == 2
    assert following["snapshot"]["supertopic_count"] == 1
    assert following["snapshot"]["completed_at"]

    assert len(following["items"]) == 3
    blogger_a = next(item for item in following["items"] if item["object_id"] == "1000000001")
    assert blogger_a["object_type"] == "blogger"
    assert blogger_a["display_name"] == "博主甲"
    assert blogger_a["page_url"] == "https://weibo.com/u/1000000001"
    assert blogger_a["app_scheme"] == ""
    assert blogger_a["source_order"] == 0
    assert blogger_a["platform_followed_at"] == ""

    assert len(following["relationships"]) == 3
    rel_a = next(rel for rel in following["relationships"] if rel["object_id"] == "1000000001")
    assert rel_a["active"] is True
    assert rel_a["platform_followed_at"] == ""
    assert rel_a["local_first_seen_at"] == "2026-07-18T00:00:00+00:00"
    assert rel_a["last_confirmed_at"] == "2026-07-18T00:00:00+00:00"

    assert len(following["names"]) == 3
    name_a = next(name for name in following["names"] if name["object_id"] == "1000000001")
    assert name_a["name"] == "博主甲"
    assert name_a["current"] is True

    assert not following["changes"]


def test_following_payload_is_none_when_archive_has_no_following_data(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))

    snapshot = ArchiveRenderSnapshot.from_repository(repo)

    assert snapshot.payload()["following"] is None
    assert len(snapshot.posts) == 1


def test_following_payload_groups_changes_by_snapshot_with_counts(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    first_items = [
        _object("blogger", "1000000001", display_name="博主甲", source_order=0),
    ]
    _commit(repo, first_items, cutoff="2026-07-18T00:00:00+00:00")
    second_items = [
        _object("blogger", "1000000001", display_name="博主甲改名", source_order=0),
        _object("blogger", "1000000002", display_name="博主乙", source_order=1),
    ]
    _commit(repo, second_items, cutoff="2026-07-19T00:00:00+00:00")

    following = ArchiveRenderSnapshot.from_repository(repo).payload()["following"]

    assert following is not None
    assert len(following["changes"]) == 1
    change = following["changes"][0]
    assert change["followed"] == 1
    assert change["unfollowed"] == 0
    assert change["renamed"] == 1
    assert change["refollowed"] == 0


def _render(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    return repo, root


def _render_all(tmp_path, following_items=None, cutoff="2026-07-18T00:00:00+00:00"):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _render(tmp_path)
    if following_items is not None:
        _commit(repo, following_items, cutoff=cutoff)
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, path: path.write_bytes(b"pdf")
    )
    html = (root / "微博书.html").read_text(encoding="utf-8")
    data = (root / "data" / "archive-data.js").read_text(encoding="utf-8")
    return html, data


def test_following_html_has_dual_tabs_with_feed_default(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    assert "微博正文" in html
    assert "关注资料" in html
    assert 'data-tab="feed"' in html
    assert 'data-tab="following"' in html
    assert 'data-tab-panel="feed"' in html
    assert 'data-tab-panel="following"' in html
    for token in ("fetch(", "XMLHttpRequest", "WebSocket"):
        assert token not in html


def test_following_summary_data_in_archive_data_source(tmp_path):
    _html, data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
            _object("blogger", "1000000002", display_name="博主乙", source_order=1),
            _object("supertopic", "1022:100808test", display_name="测试超话"),
        ],
        cutoff="2026-07-18T00:00:00+00:00",
    )

    assert '"following":{' in data
    assert "2026-07-18T00:00:00+00:00" in data
    assert '"blogger_count":2' in data
    assert '"supertopic_count":1' in data
    assert '"status":"complete"' in data


def test_following_empty_state_when_archive_has_no_following_data(tmp_path):
    html, data = _render_all(tmp_path)

    assert '"following":null' in data
    assert "尚未建立关注资料" in html


def test_following_three_column_structure(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
            _object("supertopic", "1022:100808test", display_name="测试超话"),
        ],
    )

    assert "data-following-categories" in html
    assert "data-following-list" in html
    assert "data-following-detail" in html
    for token in ("关注博主", "关注超话", "历史变化"):
        assert token in html


def test_following_detail_fields_and_duration_source(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    for token in (
        "当前名称", "数字身份", "关注状态", "关注时长", "时长来源",
        "截止时间", "本地首次发现", "最后确认",
        "自本地首次记录起至少", "本地最短记录",
    ):
        assert token in html


def test_following_mobile_fold_navigation(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    assert "data-following-level" in html
    assert "max-width:899px" in html


def test_following_sort_controls(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    assert "真实关注日期：最新优先" in html
    assert "微博返回顺序" in html
    assert "名称顺序" in html
    assert "本地首次记录" in html
    assert "真实日期未知" in html


def test_following_identity_actions(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    for token in ("打开主页", "复制主页链接", "复制数字身份", "查看名称记录"):
        assert token in html


def test_following_supertopic_identity_actions_include_app_scheme_entry(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("supertopic", "1022:100808test", display_name="测试超话"),
        ],
    )

    assert "打开应用" in html


def test_following_mobile_starts_from_category_layer(tmp_path):
    _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 844})
        page.goto((tmp_path / "archive" / "微博书.html").resolve().as_uri(), wait_until="load")
        page.locator('[data-tab="following"]').click()

        layout = page.locator(".following-layout")
        assert layout.get_attribute("data-following-level") == "category"
        assert page.locator("[data-following-categories]").is_visible()
        assert page.locator("[data-following-list]").is_hidden()
        browser.close()


@pytest.mark.parametrize("viewport_width", [375, 390, 430, 680, 1280])
def test_following_viewports_are_offline_and_have_no_horizontal_overflow(
    tmp_path, viewport_width
):
    _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
            _object("supertopic", "1022:100808test", display_name="测试超话"),
        ],
    )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("本机未安装 Playwright Chromium")
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": viewport_width, "height": 844})
        requests: list[str] = []
        page_errors: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto((tmp_path / "archive" / "微博书.html").resolve().as_uri(), wait_until="load")
        page.locator('[data-tab="following"]').click()

        if viewport_width < 900:
            assert page.locator("[data-following-categories]").is_visible()
            assert page.locator("[data-following-list]").is_hidden()
            page.locator('[data-category="blogger"]').click()
            assert page.locator("[data-following-list]").is_visible()
            page.locator(".following-item").first.click()
            assert page.locator("[data-following-detail]").is_visible()
        else:
            assert page.locator("[data-following-categories]").is_visible()
            assert page.locator("[data-following-list]").is_visible()
            assert page.locator("[data-following-detail]").is_visible()

        metrics = page.evaluate("""() => ({
            viewport: window.innerWidth,
            documentWidth: document.documentElement.scrollWidth
        })""")
        assert metrics["documentWidth"] <= metrics["viewport"]
        assert all(request.startswith("file://") for request in requests)
        assert page_errors == []
        browser.close()


def test_following_latest_changes_summary(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    assert "最新变化" in html
    assert "首次建立关注资料" in html
    for token in ("新增关注", "取消关注", "名称变化", "重新关注"):
        assert token in html


def test_following_html_offline_no_network_tokens(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    for token in ("fetch(", "XMLHttpRequest", "WebSocket", "http://", "https://", "Cookie"):
        assert token not in html


def test_following_markdown_excludes_following_structure(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer

    repo, root = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    _commit(repo, [_object("blogger", "1000000001", display_name="博主甲")])
    ArchiveRenderer(repo).render_all(
        root, render_pdf=lambda _html, path: path.write_bytes(b"pdf")
    )
    markdown = (root / "微博书.md").read_text(encoding="utf-8")

    for token in ("data-tab=", "following-view", "关注资料快照", "following-layout", "关注资料"):
        assert token not in markdown


def test_following_visual_contract_checklist(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
            _object("supertopic", "1022:100808test", display_name="测试超话"),
        ],
    )

    assert "微博正文" in html and "关注资料" in html
    assert 'data-tab="feed"' in html and 'data-tab="following"' in html
    assert "关注资料快照" in html and "截止时间" in html
    assert "data-following-categories" in html
    assert "data-following-list" in html
    assert "data-following-detail" in html
    assert "真实关注日期：最新优先" in html
    assert "打开主页" in html and "复制主页链接" in html
    assert "复制数字身份" in html and "查看名称记录" in html
    assert "最新变化" in html and "历史变化" in html


def test_following_payload_projects_unconfirmed_bloggers(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot

    repo, _ = _repo(tmp_path)
    repo.upsert_post(_post("A", "2026-07-14T01:00:00+00:00"))
    _commit(
        repo,
        [
            _object("blogger", "1000000001", display_name="博主甲"),
            _object("blogger", "1000000002", display_name="博主乙", source_order=1),
        ],
    )
    snapshot_id = repo.begin_following_snapshot("2026-07-19T00:00:00+00:00")
    repo.stage_following_items(
        snapshot_id,
        [_object("blogger", "1000000001", display_name="博主甲")],
    )
    repo.commit_following_snapshot(
        snapshot_id,
        cutoff_at="2026-07-19T01:00:00+00:00",
        bloggers_complete=True,
        supertopics_complete=True,
        blogger_reported_total=2,
        supertopic_reported_total=0,
        blogger_unconfirmed=True,
    )

    following = ArchiveRenderSnapshot.from_repository(repo).payload()["following"]

    assert following["snapshot"]["blogger_unconfirmed"] is True
    unconfirmed = following["snapshot"]["unconfirmed_bloggers"]
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["object_id"] == "1000000002"
    assert unconfirmed[0]["name"] == "博主乙"
    assert unconfirmed[0]["page_url"] == "https://weibo.com/u/1000000002"


def test_following_unconfirmed_ui_tokens(tmp_path):
    html, _data = _render_all(
        tmp_path,
        following_items=[
            _object("blogger", "1000000001", display_name="博主甲"),
        ],
    )

    assert "状态未确认" in html
    assert "following-item-unconfirmed" in html
    assert "unconfirmed_bloggers" in html
