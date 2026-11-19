"""本人微博主页的增量发现规则。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class ProfileItem:
    bid: str
    is_pinned: bool = False
    pin_order: int | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.bid:
            raise ValueError("BID 不能为空")


@dataclass(frozen=True)
class ProfilePage:
    items: list[ProfileItem]
    is_last: bool


@dataclass(frozen=True)
class DiscoveryResult:
    new_bids: list[str]
    refresh_bids: list[str]
    exhausted: bool
    profile_metadata: dict[str, ProfileItem] = field(default_factory=dict, compare=False)


def page_reaches_cutoff(page: ProfilePage, cutoff: date | None) -> bool:
    """页内最老（最后一条）带时间的非置顶条目是否早于 ``cutoff``。

    置顶条目不参与判定（置顶永远挂第 1 页，与时间顺序无关）；没有
    可判定时间的页不触发截止。
    """
    if cutoff is None:
        return False
    for item in reversed(page.items):
        if item.is_pinned or item.created_at is None:
            continue
        return item.created_at.date() < cutoff
    return False


def item_before_cutoff(item: ProfileItem, cutoff: date | None) -> bool:
    """条目是否早于 ``cutoff``（置顶与无时间条目一律视为不早于）。"""
    if cutoff is None or item.is_pinned or item.created_at is None:
        return False
    return item.created_at.date() < cutoff


def discover_backfill(
    pages: Iterable[ProfilePage],
    known_bids: set[str],
    cutoff: date | None,
    refresh_limit: int = 50,
) -> DiscoveryResult:
    """从最新页往老翻，补齐 ``cutoff`` 之前未建档的微博。

    已知 BID 只计数不下载（沿用 new/refresh 分流，最多复查
    ``refresh_limit`` 条）；页内最老非置顶条目早于 ``cutoff`` 即停止，
    该页内早于截止的条目不收；``cutoff`` 为 None 时翻到 ``is_last``。
    """
    if refresh_limit < 0:
        raise ValueError("refresh_limit 不能为负数")

    new_bids: list[str] = []
    refresh_bids: list[str] = []
    seen_bids: set[str] = set()
    profile_metadata: dict[str, ProfileItem] = {}

    for page in pages:
        terminal = page_reaches_cutoff(page, cutoff)
        for item in page.items:
            if item.bid in seen_bids:
                continue
            seen_bids.add(item.bid)
            if terminal and item_before_cutoff(item, cutoff):
                continue
            profile_metadata[item.bid] = item
            if item.bid in known_bids:
                if len(refresh_bids) < refresh_limit:
                    refresh_bids.append(item.bid)
            else:
                new_bids.append(item.bid)

        # 即使已到最后一页，按时间范围跳过了更早内容也不能宣称历史补全。
        if terminal:
            return DiscoveryResult(new_bids, refresh_bids, exhausted=False, profile_metadata=profile_metadata)
        if page.is_last:
            return DiscoveryResult(new_bids, refresh_bids, exhausted=True, profile_metadata=profile_metadata)

    return DiscoveryResult(new_bids, refresh_bids, exhausted=True, profile_metadata=profile_metadata)


def discover_incremental(
    pages: Iterable[ProfilePage],
    known_bids: set[str],
    refresh_limit: int = 50,
) -> DiscoveryResult:
    """发现全部新微博，并收集最多 ``refresh_limit`` 条旧微博。"""
    if refresh_limit < 0:
        raise ValueError("refresh_limit 不能为负数")

    new_bids: list[str] = []
    refresh_bids: list[str] = []
    seen_bids: set[str] = set()
    profile_metadata: dict[str, ProfileItem] = {}
    page_iterator = iter(pages)

    while True:
        try:
            page = next(page_iterator)
        except StopIteration:
            return DiscoveryResult(new_bids, refresh_bids, exhausted=True, profile_metadata=profile_metadata)

        for item in page.items:
            if item.bid in seen_bids:
                continue
            seen_bids.add(item.bid)
            profile_metadata[item.bid] = item

            if item.bid in known_bids:
                if len(refresh_bids) < refresh_limit:
                    refresh_bids.append(item.bid)
            else:
                new_bids.append(item.bid)

        if page.is_last:
            return DiscoveryResult(new_bids, refresh_bids, exhausted=True, profile_metadata=profile_metadata)
        if refresh_limit > 0 and len(refresh_bids) >= refresh_limit:
            return DiscoveryResult(new_bids, refresh_bids, exhausted=False, profile_metadata=profile_metadata)
