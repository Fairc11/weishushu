"""任务管理器：内存任务表 + 状态机 + 订阅广播。

- 单例（main.py lifespan 钩子创建）
- 状态机：pending → running → {done, error, cancelled}
- 每个任务一个 asyncio.Queue 缓冲 WS 消息
- v1.1.1 阶段 1 不要求多任务并发，但接口留好（O4 决策：单任务够用）

v1.2.0 P0 整改 (B-03 + B-04 + B-06)：
- B-03: cancel 不再"假取消"——存 _asyncio_task 真触发协程取消 + set_done/set_error 守卫
- B-04: 终态后 1 小时 GC；lifespan 兜底清空
- B-06: run_in_background 把 task 引用挂到 rec._asyncio_task，防 GC 回收
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from backend.app.platform_paths import platform_paths
from backend.app.services.persistent_task_store import (
    PersistentTaskRecord,
    PersistentTaskStore,
)
from weibo_book.archive.pacing import PacingStatus
from weibo_book.errors import OperationCancelled, OperationPaused, WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

# 终态后多久清掉（秒）
TERMINAL_GC_TTL = 3600


class TaskRecord:
    __slots__ = ("id", "state", "progress_pct", "progress_msg", "progress_event",
                 "started_at", "finished_at", "result", "error",
                 "subscribers", "lock", "_asyncio_task",
                 "_terminal_at", "_cancel_event", "_commit_gate",
                 "_commit_started", "_pause_event", "control_mode",
                 "persistent_record")

    def __init__(
        self,
        task_id: str,
        *,
        control_mode: str = "immediate",
        persistent_record: PersistentTaskRecord | None = None,
    ) -> None:
        self.id = task_id
        self.state = "pending"
        self.progress_pct = 0.0
        self.progress_msg = ""
        self.progress_event: Optional[dict] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.subscribers: list[asyncio.Queue] = []
        self.lock = asyncio.Lock()
        # B-03 + B-06：持有真正的 asyncio.Task 引用（cancel/防 GC 都要用）
        self._asyncio_task: Optional[asyncio.Task] = None
        # B-04：终态时间戳，给 GC 兜底用
        self._terminal_at: Optional[float] = None
        # 同步抓取/下载线程读取此信号，在安全检查点主动停止。
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self.control_mode = control_mode
        self.persistent_record = persistent_record
        # 固定成品提交与取消共用一个门闩，消除最后检查后的竞态。
        self._commit_gate = threading.Lock()
        self._commit_started = False

    def try_request_cancel(self) -> bool:
        """在固定成品进入提交前原子接受取消。"""
        with self._commit_gate:
            if self._commit_started:
                return False
            self._pause_event.clear()
            self._cancel_event.set()
            return True

    def try_request_pause(self) -> bool:
        """在固定成品进入提交前原子接受暂停。"""
        with self._commit_gate:
            if self._commit_started or self._cancel_event.is_set():
                return False
            self._pause_event.set()
            return True

    def try_begin_commit(self) -> bool:
        """在尚未取消时原子关闭取消入口。"""
        with self._commit_gate:
            if self._cancel_event.is_set() or self._pause_event.is_set():
                return False
            self._commit_started = True
            return True

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "progress_pct": self.progress_pct,
            "progress_msg": self.progress_msg,
            "progress_event": self.progress_event,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "task_kind": (
                self.persistent_record.task_kind
                if self.persistent_record is not None else None
            ),
            "mode": (
                self.persistent_record.mode
                if self.persistent_record is not None else None
            ),
            "output_dir": (
                self.persistent_record.output_dir
                if self.persistent_record is not None else None
            ),
            "pacing_mode": (
                self.persistent_record.pacing_mode
                if self.persistent_record is not None else None
            ),
            "pacing_state": (
                self.persistent_record.pacing_state
                if self.persistent_record is not None else None
            ),
            "pacing_request_kind": (
                self.persistent_record.pacing_request_kind
                if self.persistent_record is not None else None
            ),
            "next_wait_seconds": (
                self.persistent_record.next_wait_seconds
                if self.persistent_record is not None else None
            ),
        }


class TaskManager:
    def __init__(
        self,
        persistent_store: PersistentTaskStore | None = None,
    ) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._global_lock = asyncio.Lock()
        self._persistent_store = persistent_store
        # B-04：GC 调度表（task_id → TimerHandle），存到 self 避免被 GC
        self._gc_timers: dict[str, asyncio.TimerHandle] = {}

    async def create(self) -> str:
        async with self._global_lock:
            task_id = uuid.uuid4().hex[:12]
            self._tasks[task_id] = TaskRecord(task_id)
            logger.info("task created: %s", task_id)
            return task_id

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _persist(
        self,
        rec: TaskRecord,
        **changes: object,
    ) -> PersistentTaskRecord | None:
        if self._persistent_store is None or rec.persistent_record is None:
            return None
        updated = replace(
            rec.persistent_record,
            saved_at=self._utc_now(),
            **changes,
        )
        self._persistent_store.save(updated)
        rec.persistent_record = updated
        return updated

    async def create_personal_archive(
        self,
        *,
        mode: str,
        output_dir: str,
        expected_uid: str | None = None,
        pacing_mode: str = "standard",
        keep_awake_when_plugged: bool = False,
        target_label: str | None = None,
    ) -> str:
        if self._persistent_store is None:
            raise WeiboError(
                "持久任务存储尚未配置",
                kind=WeiboErrorKind.API,
            )
        async with self._global_lock:
            if self._persistent_store.load() is not None:
                raise WeiboError(
                    "已有未完成的本人归档任务",
                    kind=WeiboErrorKind.API,
                )
            task_id = uuid.uuid4().hex[:12]
            now = self._utc_now()
            persistent = PersistentTaskRecord(
                schema_version=7,
                task_id=task_id,
                task_kind="personal_archive",
                mode=mode,
                output_dir=str(Path(output_dir)),
                state="running",
                phase="sync",
                archive_run_id=None,
                progress_current=0,
                progress_total=None,
                progress_unit="post",
                started_at=now,
                saved_at=now,
                pause_reason="",
                saved_content="尚未提交微博",
                expected_uid=expected_uid,
                legacy_index_sha256=None,
                error_recoverable=False,
                pacing_mode=pacing_mode,
                keep_awake_when_plugged=keep_awake_when_plugged,
                pacing_state="standard" if pacing_mode == "standard" else "estimating",
                pacing_request_kind=None,
                next_wait_seconds=None,
                checkpoint={},
                target_label=target_label,
            )
            record = TaskRecord(
                task_id,
                control_mode="cooperative",
                persistent_record=persistent,
            )
            record.state = "running"
            self._persistent_store.save(persistent)
            self._tasks[task_id] = record
            logger.info("persistent personal archive task created: %s", task_id)
            return task_id

    async def create_following_archive(
        self,
        *,
        output_dir: str,
        expected_uid: str,
        snapshot_id: str,
        pacing_mode: str = "standard",
        keep_awake_when_plugged: bool = False,
    ) -> str:
        if self._persistent_store is None:
            raise WeiboError("持久任务存储尚未配置", kind=WeiboErrorKind.API)
        async with self._global_lock:
            if self._persistent_store.load() is not None:
                raise WeiboError("已有未完成的持久任务", kind=WeiboErrorKind.API)
            task_id = uuid.uuid4().hex[:12]
            now = self._utc_now()
            persistent = PersistentTaskRecord(
                schema_version=7,
                task_id=task_id,
                task_kind="following_archive",
                mode="update",
                output_dir=str(Path(output_dir)),
                state="running",
                phase="bloggers",
                archive_run_id=None,
                progress_current=0,
                progress_total=None,
                progress_unit="page",
                started_at=now,
                saved_at=now,
                pause_reason="",
                saved_content="尚未提交关注资料",
                expected_uid=expected_uid,
                legacy_index_sha256=None,
                error_recoverable=False,
                pacing_mode=pacing_mode,
                keep_awake_when_plugged=keep_awake_when_plugged,
                pacing_state="standard" if pacing_mode == "standard" else "estimating",
                pacing_request_kind=None,
                next_wait_seconds=None,
                checkpoint={
                    "snapshot_id": snapshot_id,
                    "blogger_next_page": 1,
                    "blogger_next_cursor": None,
                    "blogger_completed_count": 0,
                    "bloggers_done": False,
                    "supertopics_done": False,
                    "blogger_reported_total": None,
                    "supertopic_reported_total": None,
                },
            )
            record = TaskRecord(
                task_id,
                control_mode="cooperative",
                persistent_record=persistent,
            )
            record.state = "running"
            self._persistent_store.save(persistent)
            self._tasks[task_id] = record
            logger.info("persistent following archive task created: %s", task_id)
            return task_id

    async def discard_unstarted_following_archive(self, task_id: str) -> None:
        """回滚尚未启动工作协程的关注任务记录。"""

        async with self._global_lock:
            record = self._tasks.get(task_id)
            if (
                record is None
                or record.persistent_record is None
                or record.persistent_record.task_kind != "following_archive"
                or record.state != "running"
                or record._asyncio_task is not None
            ):
                raise WeiboError("关注资料任务已启动，不能回滚记录", kind=WeiboErrorKind.API)
            if self._persistent_store is not None:
                self._persistent_store.clear()
            del self._tasks[task_id]

    async def restore_waiting_record(
        self,
        persistent: PersistentTaskRecord,
    ) -> bool:
        if persistent.state not in {"waiting_resume", "error"}:
            return False
        async with self._global_lock:
            if persistent.task_id in self._tasks or any(
                item.persistent_record is not None
                for item in self._tasks.values()
            ):
                return False
            record = TaskRecord(
                persistent.task_id,
                control_mode="cooperative",
                persistent_record=persistent,
            )
            record.state = persistent.state
            record.progress_msg = persistent.saved_content
            record.started_at = datetime.fromisoformat(
                persistent.started_at
            ).timestamp()
            if persistent.progress_total is not None and persistent.progress_total > 0:
                record.progress_pct = min(
                    1.0,
                    persistent.progress_current / persistent.progress_total,
                )
            if persistent.state == "error":
                record.error = persistent.pause_reason
            self._tasks[persistent.task_id] = record
            logger.info("persistent task restored without worker: %s", persistent.task_id)
            return True

    async def prepare_persistent_resume(self, task_id: str) -> bool:
        rec = self._tasks.get(task_id)
        if (
            rec is None
            or rec.persistent_record is None
            or rec.state not in {"waiting_resume", "error"}
            or (
                rec.state == "error"
                and not rec.persistent_record.error_recoverable
            )
        ):
            return False
        async with rec.lock:
            if (
                rec.state not in {"waiting_resume", "error"}
                or (
                    rec.state == "error"
                    and not rec.persistent_record.error_recoverable
                )
            ):
                return False
            rec._pause_event.clear()
            rec._cancel_event.clear()
            rec._commit_started = False
            rec.state = "running"
            rec.finished_at = None
            rec.error = None
            self._persist(
                rec,
                state="running",
                pause_reason="",
                error_recoverable=False,
            )
        await self._broadcast(task_id, {"type": "running"})
        return True

    async def set_persistent_phase(self, task_id: str, phase: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        async with rec.lock:
            self._persist(rec, phase=phase)

    async def set_following_checkpoint(
        self,
        task_id: str,
        *,
        blogger_next_page: int,
        blogger_next_cursor: int | None,
        blogger_completed_count: int,
        bloggers_done: bool,
        supertopics_done: bool,
        blogger_reported_total: int | None,
        supertopic_reported_total: int | None,
    ) -> None:
        rec = self._tasks.get(task_id)
        if (
            rec is None
            or rec.persistent_record is None
            or rec.persistent_record.task_kind != "following_archive"
        ):
            raise WeiboError("关注资料持久任务不存在", kind=WeiboErrorKind.API)
        current = rec.persistent_record.checkpoint
        current_page = current.get("blogger_next_page")
        if type(current_page) is not int or blogger_next_page < current_page:
            raise WeiboError("关注博主恢复页码不得倒退", kind=WeiboErrorKind.API)
        checkpoint = {
            "snapshot_id": current["snapshot_id"],
            "blogger_next_page": blogger_next_page,
            "blogger_next_cursor": blogger_next_cursor,
            "blogger_completed_count": blogger_completed_count,
            "bloggers_done": bloggers_done,
            "supertopics_done": supertopics_done,
            "blogger_reported_total": blogger_reported_total,
            "supertopic_reported_total": supertopic_reported_total,
        }
        async with rec.lock:
            self._persist(rec, checkpoint=checkpoint)

    async def set_archive_run_id(self, task_id: str, run_id: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        async with rec.lock:
            self._persist(rec, archive_run_id=run_id)

    async def set_expected_uid(self, task_id: str, expected_uid: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        if not isinstance(expected_uid, str) or not expected_uid.strip():
            raise WeiboError("可信账号标识无效", kind=WeiboErrorKind.AUTH)
        async with rec.lock:
            existing = rec.persistent_record.expected_uid
            if existing is not None and existing != expected_uid:
                raise WeiboError("当前账号与持久任务账号不一致", kind=WeiboErrorKind.AUTH)
            self._persist(rec, expected_uid=expected_uid)

    async def set_legacy_index_sha256(self, task_id: str, value: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise WeiboError("旧索引摘要无效", kind=WeiboErrorKind.PARSE)
        async with rec.lock:
            existing = rec.persistent_record.legacy_index_sha256
            if existing is not None and existing != value:
                raise WeiboError("旧索引摘要与持久任务不一致", kind=WeiboErrorKind.PARSE)
            self._persist(rec, legacy_index_sha256=value)

    async def reconcile_after_process_start(self) -> PersistentTaskRecord | None:
        if self._persistent_store is None:
            return None
        persistent = self._persistent_store.reconcile_after_process_start()
        if persistent is not None and persistent.state in {"waiting_resume", "error"}:
            await self.restore_waiting_record(persistent)
        return persistent

    def recovery_summary(self) -> dict[str, object] | None:
        persistent = None
        for record in self._tasks.values():
            if (
                record.persistent_record is not None
                and record.state in {"waiting_resume", "error"}
            ):
                persistent = record.persistent_record
                break
        if persistent is None and self._persistent_store is not None:
            persistent = self._persistent_store.load()
        if persistent is None or persistent.state not in {"waiting_resume", "error"}:
            return None
        return {
            "task_id": persistent.task_id,
            "task_kind": persistent.task_kind,
            "mode": persistent.mode,
            "state": persistent.state,
            "phase": persistent.phase,
            "progress_current": persistent.progress_current,
            "progress_total": persistent.progress_total,
            "progress_unit": persistent.progress_unit,
            "started_at": persistent.started_at,
            "saved_at": persistent.saved_at,
            "pause_reason": persistent.pause_reason,
            "saved_content": persistent.saved_content,
            "error_recoverable": persistent.error_recoverable,
            "target_label": persistent.target_label,
            "pacing_mode": persistent.pacing_mode,
            "keep_awake_when_plugged": persistent.keep_awake_when_plugged,
            "pacing_state": persistent.pacing_state,
            "pacing_request_kind": persistent.pacing_request_kind,
            "next_wait_seconds": persistent.next_wait_seconds,
        }

    def active_persistent_task(self) -> tuple[str, str] | None:
        for task_id, record in self._tasks.items():
            if (
                record.persistent_record is not None
                and record.state in {"running", "pausing", "cancelling"}
            ):
                return task_id, record.state
        return None

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def snapshot(self, task_id: str) -> Optional[dict]:
        rec = self._tasks.get(task_id)
        return rec.snapshot() if rec else None

    async def update_progress(self, task_id: str, pct: float, msg: str = "") -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        # B-03 守卫：已被取消的别再覆盖状态为 running
        if rec.state not in {"pending", "running"}:
            return
        async with rec.lock:
            # 加锁后再判一次（防 TOCTOU）
            if rec.state not in {"pending", "running"}:
                return
            rec.progress_pct = max(0.0, min(1.0, pct))
            rec.progress_msg = msg
            rec.state = "running"
            self._persist(rec, state="running", saved_content=msg or rec.progress_msg)
        await self._broadcast(task_id, {
            "type": "progress",
            "pct": rec.progress_pct,
            "msg": rec.progress_msg,
        })

    async def update_progress_event(self, task_id: str, event: dict) -> None:
        """保存并广播结构化阶段事件，同时保持旧 pct/msg 字段兼容。"""
        rec = self._tasks.get(task_id)
        if rec is None or rec.state not in {"pending", "running"}:
            return
        pct = float(event.get("pct", rec.progress_pct))
        msg = str(event.get("detail", rec.progress_msg))
        normalized = dict(event)
        normalized["pct"] = max(0.0, min(1.0, pct))
        async with rec.lock:
            if rec.state not in {"pending", "running"}:
                return
            rec.progress_pct = normalized["pct"]
            rec.progress_msg = msg
            rec.progress_event = normalized
            rec.state = "running"
            persistent_changes: dict[str, object] = {
                "state": "running",
                "saved_content": msg,
            }
            current = normalized.get("current")
            total = normalized.get("total")
            unit = normalized.get("unit")
            if type(current) is int and current >= 0:
                if total is None or (type(total) is int and total >= current):
                    persistent_changes["progress_current"] = current
                    persistent_changes["progress_total"] = total
            if isinstance(unit, str) and unit:
                persistent_changes["progress_unit"] = unit
            self._persist(rec, **persistent_changes)
        await self._broadcast(task_id, {
            "type": "progress",
            "pct": rec.progress_pct,
            "msg": rec.progress_msg,
            "event": normalized,
        })

    async def update_pacing_status(
        self,
        task_id: str,
        status: PacingStatus,
    ) -> None:
        """只更新低强度节奏字段，不覆盖微博或文件进度。"""

        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        if rec.state not in {"pending", "running", "pausing"}:
            return
        if status.mode != rec.persistent_record.pacing_mode:
            raise ValueError("节奏状态档位与持久任务档位不一致")
        async with rec.lock:
            if rec.persistent_record is None:
                raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
            if rec.state not in {"pending", "running", "pausing"}:
                return
            if status.mode != rec.persistent_record.pacing_mode:
                raise ValueError("节奏状态档位与持久任务档位不一致")
            self._persist(
                rec,
                pacing_state=status.state,
                pacing_request_kind=status.request_kind,
                next_wait_seconds=status.next_wait_seconds,
            )
        await self._broadcast(task_id, {
            "type": "pacing",
            "mode": status.mode,
            "state": status.state,
            "request_kind": status.request_kind,
            "next_wait_seconds": status.next_wait_seconds,
            "target_min_seconds": status.target_min_seconds,
            "target_max_seconds": status.target_max_seconds,
            "disclaimer": status.disclaimer,
        })

    def _schedule_gc(self, task_id: str) -> None:
        """B-04：终态后 TERMINAL_GC_TTL 秒清掉。"""
        loop = asyncio.get_event_loop()
        # 取消旧 timer（如果有）
        old = self._gc_timers.pop(task_id, None)
        if old is not None:
            old.cancel()

        def _gc() -> None:
            rec = self._tasks.pop(task_id, None)
            self._gc_timers.pop(task_id, None)
            if rec is not None:
                logger.debug("task GC: %s", task_id)

        handle = loop.call_later(TERMINAL_GC_TTL, _gc)
        self._gc_timers[task_id] = handle

    async def set_done(self, task_id: str, result: dict) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        async with rec.lock:
            # B-03 守卫：cancelled 别被 done 覆盖
            if rec.state == "cancelled":
                return
            if rec.persistent_record is not None and self._persistent_store is not None:
                self._persistent_store.clear()
            rec.state = "done"
            rec.finished_at = time.time()
            rec.result = result
            rec.progress_pct = 1.0
            rec._terminal_at = rec.finished_at
        await self._broadcast(task_id, {"type": "done", "result": result})
        self._schedule_gc(task_id)
        logger.info("task done: %s", task_id)

    async def set_error(
        self,
        task_id: str,
        error: str,
        *,
        error_recoverable: bool = False,
    ) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        async with rec.lock:
            # B-03 守卫：cancelled 别被 error 覆盖
            if rec.state == "cancelled":
                return
            rec.state = "error"
            rec.finished_at = time.time()
            rec.error = error
            rec._terminal_at = rec.finished_at
            self._persist(
                rec,
                state="error",
                pause_reason=error,
                error_recoverable=error_recoverable,
            )
        await self._broadcast(task_id, {"type": "error", "error": error})
        if rec.persistent_record is None:
            self._schedule_gc(task_id)
        logger.error("task error: %s — %s", task_id, error)

    async def _cancel_immediately(self, task_id: str, rec: TaskRecord) -> bool:
        if not rec.try_request_cancel():
            return False
        async with rec.lock:
            rec.state = "cancelled"
            rec.finished_at = time.time()
            rec._terminal_at = rec.finished_at
        if rec._asyncio_task is not None and not rec._asyncio_task.done():
            rec._asyncio_task.cancel()
        await self._broadcast(task_id, {"type": "cancelled"})
        self._schedule_gc(task_id)
        logger.info("task cancelled: %s", task_id)
        return True

    async def request_pause(self, task_id: str) -> bool:
        rec = self._tasks.get(task_id)
        if (
            rec is None
            or rec.control_mode != "cooperative"
            or rec.state not in {"pending", "running"}
        ):
            return False
        if not rec.try_request_pause():
            return False
        async with rec.lock:
            rec.state = "pausing"
            self._persist(rec, state="pausing", pause_reason="user_requested")
        await self._broadcast(task_id, {"type": "pausing"})
        logger.info("task pause requested: %s", task_id)
        return True

    async def set_waiting_resume(
        self,
        task_id: str,
        *,
        pause_reason: str,
    ) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.control_mode != "cooperative":
            return
        async with rec.lock:
            if rec.state not in {"running", "pausing"}:
                return
            rec.state = "waiting_resume"
            rec.finished_at = time.time()
            persistent_changes: dict[str, object] = {
                "state": "waiting_resume",
                "pause_reason": pause_reason,
                "error_recoverable": False,
            }
            if (
                rec.persistent_record is not None
                and rec.persistent_record.pacing_mode != "standard"
            ):
                persistent_changes.update(
                    pacing_state="paused",
                    next_wait_seconds=None,
                )
            self._persist(
                rec,
                **persistent_changes,
            )
        await self._broadcast(task_id, {
            "type": "waiting_resume",
            "pause_reason": pause_reason,
        })
        logger.info("task waiting resume: %s", task_id)

    async def request_cancel(self, task_id: str) -> bool:
        rec = self._tasks.get(task_id)
        if rec is None or rec.state in {"done", "error", "cancelled", "abandoned"}:
            return False
        if rec.control_mode != "cooperative":
            return await self._cancel_immediately(task_id, rec)
        if not rec.try_request_cancel():
            return False
        async with rec.lock:
            rec.state = "cancelling"
            self._persist(rec, state="cancelling", pause_reason="user_cancelled")
        await self._broadcast(task_id, {"type": "cancelling"})
        logger.info("task cancel requested: %s", task_id)
        return True

    async def set_cancelled(self, task_id: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        async with rec.lock:
            if rec.persistent_record is not None and self._persistent_store is not None:
                self._persistent_store.clear()
            rec.state = "cancelled"
            rec.finished_at = time.time()
            rec._terminal_at = rec.finished_at
        await self._broadcast(task_id, {"type": "cancelled"})
        self._schedule_gc(task_id)
        logger.info("task cancelled: %s", task_id)

    async def set_abandoned(self, task_id: str) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec.persistent_record is None:
            raise WeiboError("持久任务不存在", kind=WeiboErrorKind.API)
        async with rec.lock:
            if rec.state not in {"waiting_resume", "error"}:
                raise WeiboError("当前任务状态不允许放弃", kind=WeiboErrorKind.API)
            if self._persistent_store is not None:
                self._persistent_store.clear()
            rec.state = "abandoned"
            rec.finished_at = time.time()
            rec._terminal_at = rec.finished_at
        await self._broadcast(task_id, {"type": "abandoned"})
        self._schedule_gc(task_id)

    async def cancel(self, task_id: str) -> bool:
        return await self.request_cancel(task_id)

    # ====== 订阅（WS 用）======
    async def subscribe(self, task_id: str) -> Optional[asyncio.Queue]:
        rec = self._tasks.get(task_id)
        if rec is None:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        rec.subscribers.append(q)
        # 立即发一帧 snapshot，让客户端能渲染当前状态
        await q.put({"type": "snapshot", **rec.snapshot()})
        return q

    async def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        rec = self._tasks.get(task_id)
        if rec and q in rec.subscribers:
            rec.subscribers.remove(q)

    async def _broadcast(self, task_id: str, msg: dict) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        for q in list(rec.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 满了丢最早的（不让 WS 拖慢任务）
                try:
                    _ = q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass


# 进程级单例
task_manager = TaskManager(
    persistent_store=PersistentTaskStore(platform_paths().persistent_task_file())
)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_POSIX_PATH_RE = re.compile(r"(?<!\w)/(?:[^\s/]+/)*[^\s]+")
_SECRET_RE = re.compile(
    r"\b(?:"
    r"authorization\s*[=:]\s*(?:bearer\s+)?[^\s]+"
    r"|bearer\s+[^\s]+"
    r"|[A-Za-z0-9_-]*token\s*[=:]\s*[^\s]+"
    r")",
    re.IGNORECASE,
)
_AUTH_COOKIE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(SUBP|SSOLoginState|SCF|ALF|SUB)=([^;\s]+)"
)
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def safe_task_error(exc: Exception) -> str:
    """生成可给前端的错误，完整异常只保留在日志。"""
    if not isinstance(exc, WeiboError):
        return "任务执行失败，请查看日志后重试"
    message = exc.args[0] if exc.args else ""
    if not isinstance(message, str) or not _CHINESE_RE.search(message):
        return "任务执行失败，请查看日志后重试"
    sanitized = _URL_RE.sub("[链接已隐藏]", message)
    sanitized = _WINDOWS_PATH_RE.sub("[路径已隐藏]", sanitized)
    sanitized = _POSIX_PATH_RE.sub("[路径已隐藏]", sanitized)
    sanitized = _AUTH_COOKIE_RE.sub(lambda match: f"{match.group(1)}=[已隐藏]", sanitized)
    sanitized = _SECRET_RE.sub("敏感信息已隐藏", sanitized)
    return sanitized


async def _finish_accepted_persistent_cancel(
    task_id: str,
    rec: TaskRecord | None,
    active_manager: TaskManager,
    persistent_cancel_handler: Callable[[str], Any] | None,
) -> bool:
    """优先完成已接受取消的本地安全收尾。"""
    if (
        rec is None
        or rec.persistent_record is None
        or rec.state != "cancelling"
        or persistent_cancel_handler is None
    ):
        return False
    logger.warning(
        "persistent task %s stopped after accepted cancel; finishing local cleanup",
        task_id,
    )
    try:
        await persistent_cancel_handler(task_id)
    except Exception as cleanup_error:
        logger.exception(
            "persistent task %s cancel cleanup crashed: %s",
            task_id,
            cleanup_error,
        )
        await active_manager.set_error(
            task_id,
            safe_task_error(cleanup_error),
            error_recoverable=False,
        )
    return True


# ====== 便捷函数：跑后台协程并自动管理生命周期 ======
async def run_in_background(
    task_id: str,
    coro_factory: Callable[[], Any],
    *,
    manager: TaskManager | None = None,
    persistent_cancel_handler: Callable[[str], Any] | None = None,
) -> None:
    """scraper router 用：包装 background task + 异常兜底。

    v1.2.0 P0 (B-06)：把当前协程（即本函数正在跑的 task）存到 rec._asyncio_task，
    防止 asyncio.create_task 的强引用丢失后被 GC 回收。同时供 cancel() 真触发取消。
    """
    active_manager = manager or task_manager
    rec = active_manager.get(task_id)
    current = asyncio.current_task()
    if rec is not None and current is not None:
        rec._asyncio_task = current
    try:
        result = await coro_factory()
        await active_manager.set_done(task_id, result)
    except OperationPaused as exc:
        if await _finish_accepted_persistent_cancel(
            task_id,
            rec,
            active_manager,
            persistent_cancel_handler,
        ):
            return
        await active_manager.set_waiting_resume(
            task_id,
            pause_reason=exc.pause_reason,
        )
    except OperationCancelled:
        await active_manager.set_cancelled(task_id)
    except asyncio.CancelledError:
        # 已被 cancel() 标记，不重复 set
        logger.info("task %s cancelled (协程已收到取消)", task_id)
        raise
    except Exception as e:
        if await _finish_accepted_persistent_cancel(
            task_id,
            rec,
            active_manager,
            persistent_cancel_handler,
        ):
            return
        if (
            rec is not None
            and rec.persistent_record is not None
            and isinstance(e, WeiboError)
            and e.kind in {WeiboErrorKind.AUTH, WeiboErrorKind.RATE_LIMIT}
        ):
            pause_reason = (
                "authentication_required"
                if e.kind is WeiboErrorKind.AUTH
                else "rate_limited"
            )
            logger.warning(
                "persistent task %s paused after %s",
                task_id,
                e.kind.value,
            )
            await active_manager.set_waiting_resume(
                task_id,
                pause_reason=pause_reason,
            )
            return
        logger.exception("task %s crashed: %s", task_id, e)
        await active_manager.set_error(
            task_id,
            safe_task_error(e),
            error_recoverable=(e.recoverable if isinstance(e, WeiboError) else False),
        )
