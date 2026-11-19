"""v2.1.0 分段备份（范围建档 / 历史回填）与保存格式开关。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.main import app
from backend.app.schemas import PersonalArchiveRequest
from backend.app.services.personal_archive_tasks import (
    expected_generated_file_count,
    read_archive_output_formats,
)
from weibo_book.archive.discovery import (
    ProfileItem,
    ProfilePage,
    discover_backfill,
    item_before_cutoff,
    page_reaches_cutoff,
)
from weibo_book.archive.render_snapshot import ArchiveRenderer
from weibo_book.archive.repository import ArchiveError, ArchiveRepository
from weibo_book.archive.sync import PersonalArchiveSync, _months_ago
from weibo_book.errors import WeiboError
from weibo_book.models import Post


class IdentityProvider:
    def whoami(self) -> dict:
        return {"uid": "10001", "screen_name": "测试用户"}


class FakeSource:
    def __init__(self, pages: list[ProfilePage], posts: dict[str, Post]) -> None:
        self.pages = pages
        self.posts = posts

    def iter_profile_pages(self, uid: str, **kwargs):
        yield from self.pages

    def fetch_post(self, uid: str, bid: str) -> Post:
        return self.posts[bid]

    def fetch_recent_comments(self, post_id: str, limit: int = 10) -> list:
        return []


def item(bid: str, created: date | None = None, *, pinned: bool = False) -> ProfileItem:
    created_at = (
        datetime(created.year, created.month, created.day, tzinfo=timezone.utc)
        if created is not None
        else None
    )
    return ProfileItem(bid, pinned, 1 if pinned else None, created_at)


def post(bid: str, created: date) -> Post:
    return Post(
        bid=bid,
        uid="10001",
        user_name="测试用户",
        user_avatar="avatar.jpg",
        text="正文",
        created_at=datetime(
            created.year, created.month, created.day, tzinfo=timezone.utc
        ),
    )


def known_bids(root: Path) -> set[str]:
    repository = ArchiveRepository.open(root, "10001")
    try:
        return repository.list_known_bids()
    finally:
        repository.close()


def read_meta(root: Path) -> tuple[str | None, bool]:
    repository = ArchiveRepository.open(root, "10001")
    try:
        return repository.read_history_cutoff(), repository.read_history_complete()
    finally:
        repository.close()


# ====== 月份推算 ======


def test_months_ago_clamps_to_target_month_end():
    assert _months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert _months_ago(date(2026, 1, 15), 3) == date(2025, 10, 15)
    assert _months_ago(date(2026, 9, 7), 12) == date(2025, 9, 7)
    assert _months_ago(date(2026, 5, 31), 3) == date(2026, 2, 28)


# ====== 截止判定 ======


def test_pinned_items_never_trigger_cutoff():
    cutoff = date(2026, 6, 1)
    page = ProfilePage(
        [item("P1", date(2025, 1, 1), pinned=True), item("N1", date(2026, 7, 1))],
        is_last=False,
    )
    assert not page_reaches_cutoff(page, cutoff)
    assert not item_before_cutoff(item("P1", date(2025, 1, 1), pinned=True), cutoff)
    assert not item_before_cutoff(item("X", None), cutoff)
    assert not page_reaches_cutoff(
        ProfilePage([item("P1", date(2025, 1, 1), pinned=True)], is_last=False),
        cutoff,
    )


def test_cutoff_boundary_is_inclusive():
    cutoff = date(2026, 6, 1)
    assert not item_before_cutoff(item("A", date(2026, 6, 1)), cutoff)
    assert item_before_cutoff(item("A", date(2026, 5, 31)), cutoff)
    page = ProfilePage(
        [item("A", date(2026, 7, 1)), item("B", date(2026, 6, 1))], is_last=False
    )
    assert not page_reaches_cutoff(page, cutoff)


# ====== discover_backfill ======


def test_backfill_passes_known_region_and_collects_older_news():
    cutoff = date(2026, 6, 1)
    pages = [
        ProfilePage(
            [
                item("K1", date(2026, 8, 1)),
                item("K2", date(2026, 7, 1)),
                item("C1", date(2026, 6, 15)),
            ],
            is_last=False,
        ),
        ProfilePage(
            [item("C2", date(2026, 6, 2)), item("OLD", date(2026, 5, 1))],
            is_last=False,
        ),
    ]
    result = discover_backfill(pages, {"K1", "K2"}, cutoff, refresh_limit=5)
    assert result.new_bids == ["C1", "C2"]
    assert result.refresh_bids == ["K1", "K2"]
    assert not result.exhausted


def test_backfill_refresh_cap_does_not_stop_traversal():
    known = {f"K{index}" for index in range(8)}
    pages = [
        ProfilePage([item(f"K{index}", date(2026, 8, 1)) for index in range(8)], is_last=False),
        ProfilePage([item("NEW1", date(2026, 5, 1))], is_last=True),
    ]
    result = discover_backfill(pages, known, None, refresh_limit=5)
    assert result.refresh_bids == [f"K{index}" for index in range(5)]
    assert result.new_bids == ["NEW1"]
    assert result.exhausted


def test_backfill_without_cutoff_runs_to_last_page():
    pages = [
        ProfilePage([item("A", date(2026, 8, 1))], is_last=False),
        ProfilePage([item("B", date(2020, 1, 1))], is_last=True),
    ]
    result = discover_backfill(pages, set(), None, refresh_limit=5)
    assert result.new_bids == ["A", "B"]
    assert result.exhausted


# ====== repository 元数据 ======


def test_repository_meta_defaults_and_roundtrip(tmp_path):
    repository = ArchiveRepository.create(tmp_path / "archive", "10001", "测试用户")
    assert repository.read_history_cutoff() is None
    assert repository.read_history_complete() is True
    assert repository.read_output_formats() == {"pdf": False, "markdown": False}

    repository.write_history_cutoff("2026-01-31")
    repository.write_history_complete(False)
    repository.write_output_formats(pdf=False, markdown=True)
    assert repository.read_history_cutoff() == "2026-01-31"
    assert repository.read_history_complete() is False
    assert repository.read_output_formats() == {"pdf": False, "markdown": True}

    repository.write_history_cutoff(None)
    repository.write_history_complete(True)
    assert repository.read_history_cutoff() is None
    assert repository.read_history_complete() is True
    repository.close()


def test_repository_meta_rejects_invalid_values(tmp_path):
    repository = ArchiveRepository.create(tmp_path / "archive", "10001", "测试用户")
    with pytest.raises(ArchiveError):
        repository.write_history_cutoff("2026-13-01")
    with pytest.raises(ArchiveError):
        repository.write_history_complete(1)
    with pytest.raises(ArchiveError):
        repository.write_output_formats(pdf="yes", markdown=True)
    repository.close()


# ====== 范围建档（create + range_months） ======


def test_create_with_range_stops_at_cutoff_and_writes_back_meta(tmp_path):
    today = date.today()
    recent = today - timedelta(days=10)
    old = today - timedelta(days=400)
    root = tmp_path / "archive"
    pages = [
        ProfilePage(
            [item("NEW", recent), item("OLD", old)],
            is_last=False,
        ),
    ]
    posts = {"NEW": post("NEW", recent), "OLD": post("OLD", old)}
    result = PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), range_months=3
    ).run("create")

    assert result.new_posts == 1
    assert known_bids(root) == {"NEW"}
    cutoff, complete = read_meta(root)
    assert cutoff == _months_ago(today, 3).isoformat()
    assert complete is False


def test_create_with_range_keeps_pinned_and_boundary_posts(tmp_path):
    today = date.today()
    cutoff_day = _months_ago(today, 3)
    boundary = cutoff_day
    old = today - timedelta(days=400)
    root = tmp_path / "archive"
    pages = [
        ProfilePage(
            [
                item("PIN", old, pinned=True),
                item("EDGE", boundary),
                item("OLD", old),
            ],
            is_last=False,
        ),
    ]
    posts = {
        "PIN": post("PIN", old),
        "EDGE": post("EDGE", boundary),
        "OLD": post("OLD", old),
    }
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), range_months=3
    ).run("create")

    assert known_bids(root) == {"PIN", "EDGE"}
    cutoff, complete = read_meta(root)
    assert cutoff == cutoff_day.isoformat()
    assert complete is False


def test_create_with_zero_range_collects_everything_and_marks_complete(tmp_path):
    today = date.today()
    root = tmp_path / "archive"
    pages = [ProfilePage([item("A", today - timedelta(days=900))], is_last=True)]
    posts = {"A": post("A", today - timedelta(days=900))}
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), range_months=0
    ).run("create")

    assert known_bids(root) == {"A"}
    cutoff, complete = read_meta(root)
    assert cutoff is None
    assert complete is True


def test_create_without_range_keeps_legacy_full_behavior(tmp_path):
    today = date.today()
    root = tmp_path / "archive"
    pages = [ProfilePage([item("A", today - timedelta(days=900))], is_last=True)]
    posts = {"A": post("A", today - timedelta(days=900))}
    PersonalArchiveSync(root, FakeSource(pages, posts), IdentityProvider()).run("create")

    cutoff, complete = read_meta(root)
    assert cutoff is None
    assert complete is True


def test_sync_rejects_invalid_range_values(tmp_path):
    with pytest.raises(WeiboError):
        PersonalArchiveSync(
            tmp_path / "archive",
            FakeSource([], {}),
            IdentityProvider(),
            range_months=7,
        )
    with pytest.raises(WeiboError):
        PersonalArchiveSync(
            tmp_path / "archive",
            FakeSource([], {}),
            IdentityProvider(),
            backfill_months=6,
        )


# ====== 历史回填（backfill） ======


def _create_partial_archive(tmp_path: Path) -> tuple[Path, date, date]:
    """先用近 3 个月范围建档，返回 (root, today, old_day)。"""
    today = date.today()
    recent = today - timedelta(days=10)
    old = today - timedelta(days=400)
    root = tmp_path / "archive"
    pages = [ProfilePage([item("NEW", recent), item("OLD", old)], is_last=False)]
    posts = {"NEW": post("NEW", recent), "OLD": post("OLD", old)}
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), range_months=3
    ).run("create")
    return root, today, old


def test_backfill_extends_history_and_moves_cutoff(tmp_path):
    root, today, old = _create_partial_archive(tmp_path)
    previous_cutoff = _months_ago(today, 3)
    older = previous_cutoff - timedelta(days=30)
    pages = [
        ProfilePage(
            [
                item("NEW", today - timedelta(days=10)),
                item("OLD", old),
                item("OLDER", older),
            ],
            is_last=True,
        ),
    ]
    posts = {
        "NEW": post("NEW", today - timedelta(days=10)),
        "OLD": post("OLD", old),
        "OLDER": post("OLDER", older),
    }
    result = PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), backfill_months=12
    ).run("backfill")

    assert {"OLD", "OLDER"} <= known_bids(root)
    assert result.new_posts == 2
    cutoff, complete = read_meta(root)
    # 翻到最后一页：历史补全，截止清空
    assert cutoff is None
    assert complete is True


def test_backfill_partial_window_keeps_new_cutoff(tmp_path):
    root, today, old = _create_partial_archive(tmp_path)
    previous_cutoff = _months_ago(today, 3)
    within = previous_cutoff - timedelta(days=30)
    beyond = _months_ago(previous_cutoff, 12) - timedelta(days=30)
    pages = [
        ProfilePage(
            [
                item("NEW", today - timedelta(days=10)),
                item("OLD", old),
                item("MID", within),
                item("BEYOND", beyond),
            ],
            is_last=False,
        ),
    ]
    posts = {
        "NEW": post("NEW", today - timedelta(days=10)),
        "OLD": post("OLD", old),
        "MID": post("MID", within),
        "BEYOND": post("BEYOND", beyond),
    }
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), backfill_months=12
    ).run("backfill")

    assert {"OLD", "MID"} <= known_bids(root)
    assert "BEYOND" not in known_bids(root)
    cutoff, complete = read_meta(root)
    assert cutoff == _months_ago(previous_cutoff, 12).isoformat()
    assert complete is False


def test_backfill_refuses_completed_archive(tmp_path):
    today = date.today()
    root = tmp_path / "archive"
    pages = [ProfilePage([item("A", today)], is_last=True)]
    posts = {"A": post("A", today)}
    PersonalArchiveSync(root, FakeSource(pages, posts), IdentityProvider()).run("create")

    with pytest.raises(WeiboError, match="无需回填"):
        PersonalArchiveSync(
            root, FakeSource([], {}), IdentityProvider(), backfill_months=12
        ).run("backfill")


def test_backfill_zero_months_fills_everything(tmp_path):
    root, today, old = _create_partial_archive(tmp_path)
    pages = [
        ProfilePage(
            [item("NEW", today - timedelta(days=10)), item("OLD", old)],
            is_last=True,
        ),
    ]
    posts = {
        "NEW": post("NEW", today - timedelta(days=10)),
        "OLD": post("OLD", old),
    }
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), backfill_months=0
    ).run("backfill")

    assert "OLD" in known_bids(root)
    cutoff, complete = read_meta(root)
    assert cutoff is None
    assert complete is True


def test_backfill_records_pin_observation(tmp_path):
    root, today, old = _create_partial_archive(tmp_path)
    pages = [
        ProfilePage(
            [
                item("PIN2", today - timedelta(days=5), pinned=True),
                item("NEW", today - timedelta(days=10)),
                item("OLD", old),
            ],
            is_last=True,
        ),
    ]
    posts = {
        "PIN2": post("PIN2", today - timedelta(days=5)),
        "NEW": post("NEW", today - timedelta(days=10)),
        "OLD": post("OLD", old),
    }
    PersonalArchiveSync(
        root, FakeSource(pages, posts), IdentityProvider(), backfill_months=0
    ).run("backfill")

    repository = ArchiveRepository.open(root, "10001")
    try:
        rows = repository._connection.execute(
            "SELECT bid, pin_order FROM pin_observations WHERE bid = 'PIN2'"
        ).fetchall()
    finally:
        repository.close()
    assert rows and rows[-1][1] == 1


# ====== 保存格式开关（render_all） ======


def _render_repo(tmp_path: Path) -> tuple[ArchiveRepository, Path]:
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    from weibo_book.archive.schema import PostRecord

    repository.upsert_post(PostRecord(bid="A", uid="10001", text="正文"))
    return repository, root


def test_render_all_skips_markdown_and_pdf_without_touching_stale_files(tmp_path):
    repository, root = _render_repo(tmp_path)
    stale_markdown = root / "微博书.md"
    stale_markdown.write_text("旧内容", encoding="utf-8")
    rendered = ArchiveRenderer(repository).render_all(
        root, include_pdf=False, include_markdown=False
    )
    repository.close()

    assert set(rendered) == {"html", "data"}
    assert (root / "微博书.html").is_file()
    # 跳过的格式不产出也不删旧文件
    assert stale_markdown.read_text(encoding="utf-8") == "旧内容"


def test_render_all_default_produces_markdown(tmp_path):
    repository, root = _render_repo(tmp_path)
    rendered = ArchiveRenderer(repository).render_all(root, include_pdf=False)
    repository.close()

    assert set(rendered) == {"html", "markdown", "data"}
    assert (root / "微博书.md").is_file()


def test_expected_generated_file_count():
    assert expected_generated_file_count({"pdf": True, "markdown": True}) == 4
    assert expected_generated_file_count({"pdf": False, "markdown": True}) == 3
    assert expected_generated_file_count({"pdf": False, "markdown": False}) == 2


def test_output_format_card_folds_pdf_and_markdown_default_off():
    index_html = (
        Path(__file__).resolve().parents[1]
        / "backend" / "app" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert '<details class="fmt-more"' in index_html
    pdf_row = index_html.split('id="fmt-pdf-row"', 1)[1].split("</label>", 1)[0]
    assert 'id="fmt-pdf"' in pdf_row
    assert "checked" not in pdf_row
    markdown_row = index_html.split('id="fmt-markdown-row"', 1)[1].split("</label>", 1)[0]
    assert 'id="fmt-markdown"' in markdown_row
    assert "checked" not in markdown_row


def test_read_archive_output_formats_defaults_when_missing(tmp_path):
    assert read_archive_output_formats(str(tmp_path / "missing"), "10001") == {
        "pdf": False,
        "markdown": False,
    }
    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "测试用户")
    repository.write_output_formats(pdf=False, markdown=True)
    repository.close()
    assert read_archive_output_formats(str(root), "10001") == {
        "pdf": False,
        "markdown": True,
    }


# ====== 请求模型校验 ======


def _request(mode: str, **extra) -> PersonalArchiveRequest:
    return PersonalArchiveRequest(
        output_dir="/tmp/archive",
        mode=mode,
        pacing_mode="standard",
        keep_awake_when_plugged=False,
        **extra,
    )


def test_request_backfill_requires_backfill_months():
    with pytest.raises(ValidationError):
        _request("backfill")
    with pytest.raises(ValidationError):
        _request("backfill", backfill_months=6)
    assert _request("backfill", backfill_months=12).backfill_months == 12
    assert _request("backfill", backfill_months=0).backfill_months == 0


def test_request_range_months_only_for_create():
    with pytest.raises(ValidationError):
        _request("incremental", range_months=12)
    with pytest.raises(ValidationError):
        _request("create", range_months=7)
    assert _request("create", range_months=12).range_months == 12
    assert _request("create").range_months is None


def test_request_backfill_months_only_for_backfill():
    with pytest.raises(ValidationError):
        _request("create", backfill_months=12)
    with pytest.raises(ValidationError):
        _request("rebuild", range_months=12, backfill_months=12)


# ====== 设置与预览端点 ======


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _identity() -> dict[str, str]:
    return {"uid": "10001", "screen_name": "本人"}


def test_backup_settings_get_and_save_roundtrip(client, tmp_path):
    from unittest.mock import patch

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    repository.close()

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()):
        response = client.get(
            "/api/backup/settings", params={"output_dir": str(root)}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "archive"
        assert body["output_formats"] == {"pdf": False, "markdown": False}
        assert body["history_complete"] is True
        assert body["history_cutoff"] == ""

        response = client.post(
            "/api/backup/settings",
            json={
                "output_dir": str(root),
                "output_formats": {"pdf": False, "markdown": True},
            },
        )
        assert response.status_code == 200
        assert response.json()["output_formats"] == {
            "pdf": False,
            "markdown": True,
        }

        response = client.get(
            "/api/backup/settings", params={"output_dir": str(root)}
        )
        assert response.json()["output_formats"] == {
            "pdf": False,
            "markdown": True,
        }


def test_backup_settings_save_requires_existing_archive(client, tmp_path):
    from unittest.mock import patch

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()):
        response = client.post(
            "/api/backup/settings",
            json={
                "output_dir": str(tmp_path / "empty"),
                "output_formats": {"pdf": True, "markdown": True},
            },
        )
    assert response.status_code == 409


def test_backup_preview_generates_only_html_and_data(client, tmp_path):
    from unittest.mock import patch

    root = tmp_path / "archive"
    repository = ArchiveRepository.create(root, "10001", "本人")
    from weibo_book.archive.schema import PostRecord

    repository.upsert_post(PostRecord(bid="A", uid="10001", text="正文"))
    repository.close()

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()):
        response = client.post(
            "/api/backup/preview", json={"output_dir": str(root)}
        )
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        deadline = __import__("time").time() + 15
        snapshot = None
        while __import__("time").time() < deadline:
            from backend.app.services.task_manager import task_manager

            snapshot = task_manager.snapshot(task_id)
            if snapshot and snapshot["state"] in {"done", "error", "cancelled"}:
                break
            __import__("time").sleep(0.02)
        assert snapshot is not None and snapshot["state"] == "done", snapshot
        generated = snapshot["result"]["generated_files"]
        assert any(path.endswith("微博书.html") for path in generated)
        assert any(path.endswith("archive-data.js") for path in generated)
        assert not any(path.endswith(".pdf") for path in generated)
        assert not any(path.endswith(".md") for path in generated)
        assert (root / "微博书.html").is_file()
        assert not (root / "微博书.pdf").exists()


def test_backup_preview_requires_existing_archive(client, tmp_path):
    from unittest.mock import patch

    with patch("backend.app.routers.router_backup.whoami", return_value=_identity()):
        response = client.post(
            "/api/backup/preview", json={"output_dir": str(tmp_path / "empty")}
        )
    assert response.status_code == 409
