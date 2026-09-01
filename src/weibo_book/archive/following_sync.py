"""关注资料暂存、恢复、完整提交和本地时长查询。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from weibo_book.errors import (
    OperationCancelled,
    OperationPaused,
    WeiboError,
    WeiboErrorKind,
)

from .following import FollowingCommitResult, FollowingObjectRecord
from .following_source import FollowingSource
from .pacing import AdaptiveRequestScheduler
from .repository import ArchiveRepository


@dataclass(frozen=True)
class FollowingDurationResult:
    object_type: str
    object_id: str
    source: str
    platform_followed_at: str
    local_first_seen_at: str
    last_confirmed_at: str
    currently_following: bool


def check_following_duration(
    repository: ArchiveRepository,
    object_type: str,
    object_id: str,
) -> FollowingDurationResult:
    if object_type not in {"blogger", "supertopic"}:
        raise WeiboError("关注对象类型无效", kind=WeiboErrorKind.API)
    if not isinstance(object_id, str) or not object_id:
        raise WeiboError("关注对象稳定身份不能为空", kind=WeiboErrorKind.API)
    matches = [
        row for row in repository.list_following_relationships()
        if row.object_type == object_type and row.object_id == object_id
    ]
    if not matches:
        raise WeiboError("本地档案未记录该关注对象", kind=WeiboErrorKind.NOT_FOUND)
    relationship = matches[-1]
    if relationship.platform_followed_at:
        source = "platform"
    else:
        source = "local_minimum"
    return FollowingDurationResult(
        object_type=object_type,
        object_id=object_id,
        source=source,
        platform_followed_at=relationship.platform_followed_at,
        local_first_seen_at=relationship.local_first_seen_at,
        last_confirmed_at=relationship.last_confirmed_at,
        currently_following=relationship.ended_snapshot_id is None,
    )


class FollowingArchiveSync:
    def __init__(
        self,
        repository: ArchiveRepository,
        source: FollowingSource,
        *,
        checkpoint_saved: Callable[[dict[str, object]], None] | None = None,
        phase_changed: Callable[[str], None] | None = None,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        pacing_scheduler: AdaptiveRequestScheduler | None = None,
        begin_commit: Callable[[], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.source = source
        self.checkpoint_saved = checkpoint_saved
        self.phase_changed = phase_changed
        self.progress_callback = progress_callback
        self.cancel_requested = cancel_requested or (lambda: False)
        self.pause_requested = pause_requested or (lambda: False)
        self.pacing_scheduler = pacing_scheduler
        self.begin_commit = begin_commit
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _check_interruption(self) -> None:
        if self.cancel_requested():
            raise OperationCancelled("任务已取消")
        if self.pause_requested():
            raise OperationPaused("任务已暂停", pause_reason="user_requested")

    def _phase(self, value: str) -> None:
        if self.phase_changed is not None:
            self.phase_changed(value)

    def _progress(self, phase: str, current: int, total: int | None, detail: str) -> None:
        if self.progress_callback is not None:
            pct = 0.0 if total in {None, 0} else min(1.0, current / total)
            self.progress_callback({
                "phase": phase,
                "pct": pct,
                "detail": detail,
                "current": current,
                "total": total,
                "unit": "item" if phase == "duration" else "page",
            })

    def _save_checkpoint(self, checkpoint: dict[str, object], **changes: object) -> None:
        checkpoint.update(changes)
        if self.checkpoint_saved is not None:
            self.checkpoint_saved(dict(checkpoint))

    def _network_call(self, operation):
        self._check_interruption()
        try:
            result = (
                self.pacing_scheduler.run("profile", operation)
                if self.pacing_scheduler is not None
                else operation()
            )
        except WeiboError as exc:
            if exc.kind is WeiboErrorKind.AUTH:
                raise OperationPaused(
                    "登录状态已失效，任务已暂停",
                    pause_reason="authentication_required",
                ) from exc
            if exc.kind is WeiboErrorKind.RATE_LIMIT:
                raise OperationPaused(
                    "请求受到限流，任务已暂停",
                    pause_reason="rate_limited",
                ) from exc
            if exc.kind is WeiboErrorKind.NETWORK:
                raise OperationPaused(
                    "网络不可用，任务已暂停",
                    pause_reason="network_unavailable",
                ) from exc
            raise
        self._check_interruption()
        return result

    @staticmethod
    def _completed_result(
        repository: ArchiveRepository,
        snapshot_id: str,
    ) -> FollowingCommitResult:
        snapshot = repository.get_following_snapshot(snapshot_id)
        items = repository.list_following_snapshot_items(snapshot_id)
        changes = [
            item for item in repository.list_following_changes()
            if item.snapshot_id == snapshot_id
        ]
        counts = {kind: 0 for kind in ("followed", "unfollowed", "renamed", "refollowed")}
        for item in changes:
            counts[item.change_type] += 1
        unconfirmed = snapshot.summary.get("unconfirmed_count", 0)
        return FollowingCommitResult(
            snapshot_id=snapshot_id,
            initial=repository.is_initial_following_snapshot(snapshot_id),
            blogger_count=sum(item.object_type == "blogger" for item in items),
            supertopic_count=sum(item.object_type == "supertopic" for item in items),
            followed_count=counts["followed"],
            unfollowed_count=counts["unfollowed"],
            renamed_count=counts["renamed"],
            refollowed_count=counts["refollowed"],
            unconfirmed_count=unconfirmed if type(unconfirmed) is int else 0,
        )

    def run(self, checkpoint: dict[str, object]) -> FollowingCommitResult:
        snapshot_id = checkpoint.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise WeiboError("关注资料恢复点缺少快照标识", kind=WeiboErrorKind.PARSE)
        snapshot = self.repository.get_following_snapshot(snapshot_id)
        if snapshot.status == "complete":
            return self._completed_result(self.repository, snapshot_id)
        if snapshot.status != "staging":
            raise WeiboError("关注资料暂存快照状态无效", kind=WeiboErrorKind.PARSE)

        if checkpoint.get("bloggers_done") is not True:
            self._phase("bloggers")
            self._run_bloggers(checkpoint)
        if checkpoint.get("supertopics_done") is not True:
            self._phase("supertopics")
            self._run_supertopics(checkpoint)

        self._check_interruption()
        blogger_total = checkpoint.get("blogger_reported_total")
        supertopic_total = checkpoint.get("supertopic_reported_total")
        if type(blogger_total) is not int or type(supertopic_total) is not int:
            raise WeiboError("关注资料恢复点缺少报告总数", kind=WeiboErrorKind.PARSE)
        if self.begin_commit is not None and not self.begin_commit():
            self._check_interruption()
            raise OperationCancelled("任务已取消")
        cutoff_at = self.now().astimezone(timezone.utc).isoformat()
        result = self.repository.commit_following_snapshot(
            snapshot_id,
            cutoff_at=cutoff_at,
            bloggers_complete=True,
            supertopics_complete=True,
            blogger_reported_total=blogger_total,
            supertopic_reported_total=supertopic_total,
            blogger_unconfirmed=checkpoint.get("blogger_unconfirmed") is True,
        )
        self._phase("duration")
        items = self.repository.list_following_snapshot_items(snapshot_id)
        for index, _item in enumerate(items, 1):
            self._check_interruption()
            self._progress("duration", index, len(items), "正在确认本地最短关注记录")
        return result

    def _run_bloggers(self, checkpoint: dict[str, object]) -> None:
        page = checkpoint.get("blogger_next_page")
        cursor = checkpoint.get("blogger_next_cursor")
        completed_count = checkpoint.get("blogger_completed_count")
        if type(page) is not int or page < 1:
            raise WeiboError("关注博主恢复页码无效", kind=WeiboErrorKind.PARSE)
        if cursor is not None and type(cursor) is not int:
            raise WeiboError("关注博主恢复游标无效", kind=WeiboErrorKind.PARSE)
        if type(completed_count) is not int or completed_count < 0:
            raise WeiboError("关注博主已完成数量无效", kind=WeiboErrorKind.PARSE)
        reported_total = checkpoint.get("blogger_reported_total")
        while True:
            existing = [
                item for item in self.repository.list_following_snapshot_items(
                    checkpoint["snapshot_id"]
                ) if item.object_type == "blogger"
            ]
            response = self._network_call(lambda: self.source.fetch_blogger_page(
                page=page,
                next_cursor=cursor,
                source_offset=completed_count,
            ))
            if reported_total is None:
                reported_total = response.reported_total
            elif reported_total != response.reported_total:
                raise WeiboError("关注博主跨页报告总数不一致", kind=WeiboErrorKind.PARSE)

            if len(existing) < completed_count:
                raise WeiboError("关注博主暂存数量少于恢复点", kind=WeiboErrorKind.PARSE)
            existing_by_id = {item.object_id: item for item in existing}
            response_ids = [item.object_id for item in response.items]
            duplicates = [identity for identity in response_ids if identity in existing_by_id]
            staged = response.items
            replaying_uncheckpointed_page = len(existing) > completed_count
            if replaying_uncheckpointed_page:
                existing_ids = [item.object_id for item in existing]
                if (
                    len(existing_ids) != completed_count + len(response_ids)
                    or existing_ids[completed_count:] != response_ids
                    or any(identity in existing_ids[:completed_count] for identity in response_ids)
                ):
                    raise WeiboError("关注博主崩溃重放内容与暂存页不一致", kind=WeiboErrorKind.PARSE)
                staged = [
                    replace(item, source_order=existing_by_id[item.object_id].source_order)
                    for item in response.items
                ]
            elif duplicates:
                raise WeiboError("关注博主跨页出现重复稳定身份", kind=WeiboErrorKind.PARSE)
            self.repository.stage_following_items(checkpoint["snapshot_id"], staged)
            current_items = [
                item for item in self.repository.list_following_snapshot_items(
                    checkpoint["snapshot_id"]
                ) if item.object_type == "blogger"
            ]
            if response.next_cursor == 0:
                # 报告总数包含平台不返回的条目；实际条目数偏少时进入「未确认」
                # 模式，缺失博主保留关注关系并单独标记，不计入取消关注。条目数
                # 多于报告总数仍然视为异常。完整性以条目数对比为准，
                # has_filtered_attentions 多次读取结果不稳定，不作判定依据。
                unconfirmed = len(current_items) < reported_total
                if len(current_items) > reported_total:
                    raise WeiboError(
                        "关注博主条目数与报告总数不一致",
                        kind=WeiboErrorKind.PARSE,
                    )
                self._save_checkpoint(
                    checkpoint,
                    blogger_next_page=page,
                    blogger_next_cursor=0,
                    blogger_completed_count=len(current_items),
                    bloggers_done=True,
                    blogger_reported_total=reported_total,
                    blogger_unconfirmed=unconfirmed,
                )
                self._progress("bloggers", page, page, "关注博主清单已完整暂存")
                return
            if self.pacing_scheduler is not None:
                self.pacing_scheduler.add_known_requests("profile", 1)
            page += 1
            cursor = response.next_cursor
            completed_count = len(current_items)
            self._save_checkpoint(
                checkpoint,
                blogger_next_page=page,
                blogger_next_cursor=cursor,
                blogger_completed_count=completed_count,
                blogger_reported_total=reported_total,
            )
            self._progress("bloggers", page - 1, None, "已暂存一页关注博主")
            self._check_interruption()

    def _run_supertopics(self, checkpoint: dict[str, object]) -> None:
        result = self._network_call(self.source.fetch_supertopics)
        if not result.complete:
            raise WeiboError("关注超话清单不完整", kind=WeiboErrorKind.PARSE)
        self.repository.stage_following_items(checkpoint["snapshot_id"], result.items)
        self._save_checkpoint(
            checkpoint,
            supertopics_done=True,
            supertopic_reported_total=result.reported_total,
        )
        self._progress("supertopics", 1, 1, "关注超话清单已完整暂存")
