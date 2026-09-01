"""本人微博主页的增量发现规则。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProfileItem:
    bid: str
    is_pinned: bool = False
    pin_order: int | None = None

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
