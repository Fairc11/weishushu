"""关注资料档案的本地记录类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


FOLLOWING_OBJECT_TYPES = frozenset({"blogger", "supertopic"})
FOLLOWING_CHANGE_TYPES = frozenset(
    {"followed", "unfollowed", "renamed", "refollowed"}
)


@dataclass(frozen=True)
class FollowingObjectRecord:
    object_type: str
    object_id: str
    display_name: str
    page_url: str
    app_scheme: str
    source_order: int
    platform_followed_at: str = ""

    def __post_init__(self) -> None:
        if self.object_type not in FOLLOWING_OBJECT_TYPES:
            raise ValueError("关注对象类型无效")
        if not isinstance(self.object_id, str) or not self.object_id:
            raise ValueError("关注对象稳定身份不能为空")
        if not isinstance(self.display_name, str) or not self.display_name:
            raise ValueError("关注对象名称不能为空")
        if not isinstance(self.page_url, str) or not isinstance(self.app_scheme, str):
            raise ValueError("关注对象页面入口类型无效")
        if not isinstance(self.platform_followed_at, str):
            raise ValueError("微博原始关注时间类型无效")
        if (
            isinstance(self.source_order, bool)
            or not isinstance(self.source_order, int)
            or self.source_order < 0
        ):
            raise ValueError("关注对象返回次序无效")


@dataclass(frozen=True)
class FollowingSnapshotRecord:
    snapshot_id: str
    status: str
    started_at: str
    cutoff_at: str
    bloggers_complete: bool
    supertopics_complete: bool
    blogger_reported_total: int | None
    supertopic_reported_total: int | None
    completed_at: str
    summary: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FollowingRelationshipRecord:
    relationship_id: int
    object_type: str
    object_id: str
    started_snapshot_id: str
    ended_snapshot_id: str | None
    local_first_seen_at: str
    last_confirmed_at: str
    platform_followed_at: str


@dataclass(frozen=True)
class FollowingNameRecord:
    name_record_id: int
    object_type: str
    object_id: str
    name: str
    started_snapshot_id: str
    ended_snapshot_id: str | None
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class FollowingChangeRecord:
    change_id: int
    snapshot_id: str
    change_type: str
    object_type: str
    object_id: str
    before: dict[str, object]
    after: dict[str, object]


@dataclass(frozen=True)
class FollowingCommitResult:
    snapshot_id: str
    initial: bool
    blogger_count: int
    supertopic_count: int
    followed_count: int
    unfollowed_count: int
    renamed_count: int
    refollowed_count: int
    unconfirmed_count: int = 0
