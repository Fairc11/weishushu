"""本人微博增量发现规则的回归测试。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from weibo_book.archive.discovery import (
    DiscoveryResult,
    ProfileItem,
    ProfilePage,
    discover_incremental,
)


def _page(
    *bids: str,
    is_last: bool = False,
    pinned: set[str] | None = None,
) -> ProfilePage:
    pinned_bids = pinned or set()
    return ProfilePage(
        items=[ProfileItem(bid=bid, is_pinned=bid in pinned_bids) for bid in bids],
        is_last=is_last,
    )


class CountingPages(Iterator[ProfilePage]):
    def __init__(self, pages: list[ProfilePage]) -> None:
        self._pages = iter(pages)
        self.consumed = 0

    def __iter__(self) -> CountingPages:
        return self

    def __next__(self) -> ProfilePage:
        page = next(self._pages)
        self.consumed += 1
        return page


def test_profile_item_rejects_empty_bid() -> None:
    with pytest.raises(ValueError, match="BID"):
        ProfileItem(bid="")


def test_negative_refresh_limit_is_rejected_without_consuming_pages() -> None:
    pages = CountingPages([_page("old-1")])

    with pytest.raises(ValueError, match="refresh_limit"):
        discover_incremental(pages, {"old-1"}, refresh_limit=-1)

    assert pages.consumed == 0


def test_zero_refresh_limit_still_discovers_new_posts_until_last_page() -> None:
    pages = CountingPages(
        [
            _page("old-1", "new-1"),
            _page("new-2", "old-2", is_last=True),
            _page("must-not-consume"),
        ]
    )

    result = discover_incremental(
        pages,
        {"old-1", "old-2"},
        refresh_limit=0,
    )

    assert result == DiscoveryResult(
        new_bids=["new-1", "new-2"],
        refresh_bids=[],
        exhausted=True,
    )
    assert pages.consumed == 2


def test_empty_iterator_is_exhausted() -> None:
    assert discover_incremental(iter(()), set(), refresh_limit=50) == DiscoveryResult(
        new_bids=[], refresh_bids=[], exhausted=True
    )


def test_discovers_more_than_eighty_new_posts_then_fifty_known_posts() -> None:
    new_bids = [f"new-{index}" for index in range(1, 81)]
    old_bids = [f"old-{index}" for index in range(1, 61)]
    pages = [
        _page(*new_bids[:40]),
        _page(*new_bids[40:]),
        _page(*old_bids),
        _page("must-not-consume", is_last=True),
    ]

    result = discover_incremental(pages, set(old_bids), refresh_limit=50)

    assert result == DiscoveryResult(
        new_bids=new_bids,
        refresh_bids=old_bids[:50],
        exhausted=False,
    )


def test_known_pinned_post_does_not_hide_new_post_later_in_same_page() -> None:
    pages = [_page("old-pin", "new-1", "old-1", pinned={"old-pin"})]

    result = discover_incremental(
        pages,
        {"old-pin", "old-1"},
        refresh_limit=1,
    )

    assert result.new_bids == ["new-1"]
    assert result.refresh_bids == ["old-pin"]
    assert result.exhausted is False


def test_finishes_current_page_after_refresh_limit_to_collect_later_new_posts() -> None:
    pages = CountingPages(
        [
            _page("old-1", "new-after-limit", "old-2"),
            _page("must-not-consume", is_last=True),
        ]
    )

    result = discover_incremental(
        pages,
        {"old-1", "old-2"},
        refresh_limit=1,
    )

    assert result == DiscoveryResult(
        new_bids=["new-after-limit"],
        refresh_bids=["old-1"],
        exhausted=False,
    )
    assert pages.consumed == 1


def test_stops_after_complete_non_last_page_without_probing_next_page() -> None:
    pages = CountingPages(
        [_page("old-1"), _page("must-not-consume", is_last=True)]
    )

    result = discover_incremental(pages, {"old-1"}, refresh_limit=1)

    assert result.exhausted is False
    assert pages.consumed == 1


def test_last_page_marks_exhausted_and_does_not_consume_following_page() -> None:
    pages = CountingPages(
        [_page("new-1", is_last=True), _page("must-not-consume")]
    )

    result = discover_incremental(pages, set(), refresh_limit=50)

    assert result == DiscoveryResult(
        new_bids=["new-1"], refresh_bids=[], exhausted=True
    )
    assert pages.consumed == 1


def test_natural_exhaustion_before_limit_is_reported() -> None:
    result = discover_incremental(
        [_page("new-1", "old-1")],
        {"old-1", "old-2"},
        refresh_limit=2,
    )

    assert result == DiscoveryResult(
        new_bids=["new-1"], refresh_bids=["old-1"], exhausted=True
    )


def test_duplicate_bid_across_pages_is_emitted_only_once_in_first_seen_order() -> None:
    result = discover_incremental(
        [
            _page("new-1", "old-1", "new-1"),
            _page("old-1", "new-2", "old-2", is_last=True),
        ],
        {"old-1", "old-2"},
        refresh_limit=50,
    )

    assert result == DiscoveryResult(
        new_bids=["new-1", "new-2"],
        refresh_bids=["old-1", "old-2"],
        exhausted=True,
    )


def test_new_bid_does_not_become_known_during_same_discovery() -> None:
    known_bids: set[str] = set()

    result = discover_incremental(
        [_page("new-1"), _page("new-1", is_last=True)],
        known_bids,
        refresh_limit=50,
    )

    assert result.new_bids == ["new-1"]
    assert result.refresh_bids == []
    assert known_bids == set()
