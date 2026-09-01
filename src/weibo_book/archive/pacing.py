"""本人微博归档的可中断请求节奏调度器。"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, TypeVar

from weibo_book.errors import (
    OperationCancelled,
    OperationPaused,
    WeiboError,
    WeiboErrorKind,
)

PacingMode = Literal[
    "standard",
    "low_2_3_hours",
    "low_4_6_hours",
    "low_8_12_hours",
]
RequestKind = Literal["profile", "detail", "comments", "media"]
PacingState = Literal[
    "standard",
    "estimating",
    "waiting",
    "requesting",
    "power_saving",
    "paused",
]

TARGET_WINDOWS_SECONDS: dict[str, tuple[int, int]] = {
    "low_2_3_hours": (7200, 10800),
    "low_4_6_hours": (14400, 21600),
    "low_8_12_hours": (28800, 43200),
}
REQUEST_WEIGHTS: dict[RequestKind, int] = {
    "profile": 2,
    "detail": 1,
    "comments": 2,
    "media": 1,
}

_VALID_MODES = frozenset({"standard", *TARGET_WINDOWS_SECONDS})
_VALID_REQUEST_KINDS = frozenset({"profile", "detail", "comments", "media"})
_WAIT_SLICE_SECONDS = 0.25
_POWER_RECHECK_SECONDS = 5.0
_DISCLAIMER = "目标区间不是完成时间承诺"
T = TypeVar("T")


@dataclass(frozen=True)
class PacingStatus:
    """可安全传给任务服务和界面的只读节奏状态。"""

    mode: PacingMode
    state: PacingState
    request_kind: RequestKind | None
    next_wait_seconds: float | None
    target_min_seconds: int | None
    target_max_seconds: int | None
    disclaimer: str


class AdaptiveRequestScheduler:
    """按剩余真实请求量分配目标时长，并允许暂停或取消等待。"""

    def __init__(
        self,
        mode: PacingMode,
        *,
        pause_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        uniform: Callable[[float, float], float] = random.uniform,
        wait: Callable[[float], None] = time.sleep,
        status_callback: Callable[[PacingStatus], None] | None = None,
        power_snapshot_provider: Callable[[], object] | None = None,
    ) -> None:
        self._mode = self._validate_mode(mode)
        self._pause_event = pause_event
        self._cancel_event = cancel_event
        self._monotonic = monotonic
        self._uniform = uniform
        self._wait = wait
        self._status_callback = status_callback
        self._power_snapshot_provider = power_snapshot_provider
        self._wake_probe: Callable[[], None] | None = None
        self._last_wake_generation: int | None = None
        self._remaining_by_kind: dict[RequestKind, int] | None = None
        self._started_at: float | None = None
        self._closed = False

    @property
    def mode(self) -> PacingMode:
        return self._mode

    @property
    def is_low_intensity(self) -> bool:
        return self._mode != "standard"

    def set_known_remaining(self, *, posts: int) -> None:
        """登记待处理微博数，每条先计入详情和评论两类请求。"""

        self._ensure_open()
        self._validate_nonnegative_integer(posts, field="posts")
        self.set_known_request_counts(detail=posts, comments=posts)

    def set_known_request_counts(
        self,
        *,
        profile: int = 0,
        detail: int = 0,
        comments: int = 0,
        media: int = 0,
    ) -> None:
        """登记已经确认的各类剩余真实请求数。"""

        self._ensure_open()
        counts = {
            "profile": profile,
            "detail": detail,
            "comments": comments,
            "media": media,
        }
        for kind, count in counts.items():
            self._validate_nonnegative_integer(count, field=kind)
        now = self._read_monotonic()
        if self._started_at is None:
            self._started_at = now
        self._remaining_by_kind = counts

    def add_known_requests(self, kind: RequestKind, count: int) -> None:
        """在真实分页响应确认还有后续请求时补充数量。"""

        self._ensure_open()
        request_kind = self._validate_request_kind(kind)
        self._validate_nonnegative_integer(count, field="count")
        if self._remaining_by_kind is None:
            raise ValueError("尚未登记已知剩余请求数")
        self._remaining_by_kind[request_kind] += count

    def set_wake_probe(self, probe: Callable[[], None]) -> None:
        self._ensure_open()
        self._wake_probe = probe

    def set_power_snapshot_provider(self, provider: Callable[[], object]) -> None:
        self._ensure_open()
        self._power_snapshot_provider = provider
        snapshot = provider()
        wake_generation = getattr(snapshot, "wake_generation", None)
        if type(wake_generation) is not int or wake_generation < 0:
            raise ValueError("电源状态快照无效")
        self._last_wake_generation = wake_generation

    def add_media_requests(self, count: int) -> None:
        """在详情转换完成后加入实际发现的媒体请求数。"""

        self._ensure_open()
        self._validate_nonnegative_integer(count, field="count")
        if self._remaining_by_kind is None:
            self._remaining_by_kind = {
                "profile": 0,
                "detail": 0,
                "comments": 0,
                "media": count,
            }
        else:
            self._remaining_by_kind["media"] += count

    def before_request(self, kind: RequestKind) -> None:
        """在一次真实请求前执行本档位需要的可中断等待。"""

        self._before_request(kind)

    def record_completed_request(self, kind: RequestKind) -> None:
        """记录由回调式下载器完成的一次真实请求。"""

        self._ensure_open()
        request_kind = self._validate_request_kind(kind)
        if self._mode != "standard":
            self._record_success(request_kind)

    def wait_for_retry(self, kind: RequestKind, seconds: float) -> None:
        """以可中断方式替代下游组件的固定重试休眠。"""

        self._ensure_open()
        request_kind = self._validate_request_kind(kind)
        if self._mode == "standard":
            self._wait(seconds)
            return
        multiplier = self._power_multiplier(request_kind)
        adjusted = seconds * multiplier
        self._emit_status(
            "power_saving" if multiplier > 1.0 else "waiting",
            request_kind,
            adjusted,
        )
        self._interruptible_wait(adjusted, request_kind)

    def run(self, kind: RequestKind, operation: Callable[[], T]) -> T:
        """执行一次请求；低强度模式仅对网络失败进行一次延长等待和重试。"""

        self._ensure_open()
        request_kind = self._validate_request_kind(kind)

        if self._mode == "standard":
            return operation()

        initial_wait, initial_multiplier = self._before_request(
            request_kind,
            validated=True,
        )
        try:
            result = operation()
        except WeiboError as exc:
            if exc.kind is WeiboErrorKind.AUTH:
                self._raise_paused(
                    request_kind,
                    "登录状态已失效，任务已暂停",
                    "authentication_required",
                )
            if exc.kind is WeiboErrorKind.RATE_LIMIT:
                self._raise_paused(
                    request_kind,
                    "请求受到限流，任务已暂停",
                    "rate_limited",
                )
            if exc.kind is not WeiboErrorKind.NETWORK:
                raise

            normal_initial_wait = initial_wait / initial_multiplier
            retry_multiplier = self._power_multiplier(request_kind)
            retry_wait = max(normal_initial_wait * 1.1, 1.0) * retry_multiplier
            self._require_finite(retry_wait, field="网络重试等待时长")
            self._emit_status(
                "power_saving" if retry_multiplier > 1.0 else "waiting",
                request_kind,
                retry_wait,
            )
            self._interruptible_wait(retry_wait, request_kind)
            self._power_multiplier(request_kind)
            self._emit_status("requesting", request_kind, None)
            self._check_interruption(request_kind)
            try:
                result = operation()
            except WeiboError as retry_exc:
                if retry_exc.kind is WeiboErrorKind.AUTH:
                    self._raise_paused(
                        request_kind,
                        "登录状态已失效，任务已暂停",
                        "authentication_required",
                    )
                if retry_exc.kind is WeiboErrorKind.RATE_LIMIT:
                    self._raise_paused(
                        request_kind,
                        "请求受到限流，任务已暂停",
                        "rate_limited",
                    )
                if retry_exc.kind is WeiboErrorKind.NETWORK:
                    self._raise_paused(
                        request_kind,
                        "网络仍不可用，任务已暂停",
                        "network_unavailable",
                    )
                raise

        self._record_success(request_kind)
        return result

    def close(self) -> None:
        """关闭调度器；重复关闭不产生副作用。"""

        self._closed = True

    def _before_request(
        self,
        kind: RequestKind,
        *,
        validated: bool = False,
    ) -> tuple[float, float]:
        self._ensure_open()
        request_kind = kind if validated else self._validate_request_kind(kind)

        if self._mode == "standard":
            return 0.0, 1.0

        multiplier = self._power_multiplier(request_kind)
        self._check_interruption(request_kind)
        if self._remaining_by_kind is None:
            wait_seconds = self._random_value(1.0, 3.0)
        else:
            wait_seconds = self._calculate_known_wait(request_kind)
            if wait_seconds > 0.0:
                wait_seconds *= self._random_value(0.9, 1.1)
                self._require_finite(wait_seconds, field="等待时长")
            wait_seconds *= multiplier
            self._require_finite(wait_seconds, field="电源策略等待时长")
            self._emit_status(
                "power_saving" if multiplier > 1.0 else "waiting",
                request_kind,
                wait_seconds,
            )

        if self._remaining_by_kind is None:
            wait_seconds *= multiplier
            self._require_finite(wait_seconds, field="电源策略等待时长")
            self._emit_status("estimating", request_kind, wait_seconds)

        self._interruptible_wait(wait_seconds, request_kind)
        self._power_multiplier(request_kind)
        self._emit_status("requesting", request_kind, None)
        self._check_interruption(request_kind)
        return wait_seconds, multiplier

    def _calculate_known_wait(self, request_kind: RequestKind) -> float:
        assert self._mode != "standard"
        target_min, target_max = TARGET_WINDOWS_SECONDS[self._mode]
        target_midpoint = (target_min + target_max) / 2.0
        now = self._read_monotonic()
        started_at = self._started_at
        if started_at is None:
            self._started_at = now
            started_at = now
        elapsed = now - started_at
        self._require_finite(elapsed, field="已消耗时长")
        if elapsed < 0.0:
            raise ValueError("单调时钟不得倒退")
        remaining_time = max(target_midpoint - elapsed, 0.0)
        assert self._remaining_by_kind is not None
        remaining_weight = sum(
            count * REQUEST_WEIGHTS[kind]
            for kind, count in self._remaining_by_kind.items()
        )
        request_weight = REQUEST_WEIGHTS[request_kind]
        divisor = remaining_weight if remaining_weight > 0 else request_weight
        wait_seconds = remaining_time * request_weight / divisor
        self._require_finite(wait_seconds, field="等待时长")
        return wait_seconds

    def _interruptible_wait(
        self,
        total_seconds: float,
        request_kind: RequestKind,
    ) -> None:
        self._require_finite(total_seconds, field="等待时长")
        if total_seconds < 0.0:
            raise ValueError("等待时长不得为负数")

        remaining = total_seconds
        power_elapsed = 0.0
        self._check_interruption(request_kind)
        while remaining > 0.0:
            current = min(remaining, _WAIT_SLICE_SECONDS)
            self._wait(current)
            remaining -= current
            power_elapsed += current
            if remaining < 1e-12:
                remaining = 0.0
            self._check_interruption(request_kind)
            if power_elapsed >= _POWER_RECHECK_SECONDS:
                self._power_multiplier(request_kind)
                power_elapsed = 0.0

    def _record_success(self, request_kind: RequestKind) -> None:
        if self._remaining_by_kind is None:
            return
        if self._remaining_by_kind[request_kind] > 0:
            self._remaining_by_kind[request_kind] -= 1

    def _check_interruption(self, request_kind: RequestKind) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled("任务已取消")
        if self._pause_event is not None and self._pause_event.is_set():
            self._emit_status("paused", request_kind, None)
            raise OperationPaused("任务已暂停", pause_reason="user_requested")

    def _raise_paused(
        self,
        kind: RequestKind,
        message: str,
        pause_reason: str,
    ) -> None:
        self._emit_status("paused", kind, None)
        raise OperationPaused(message, pause_reason=pause_reason)

    def _power_multiplier(self, request_kind: RequestKind) -> float:
        if self._mode == "standard" or self._power_snapshot_provider is None:
            return 1.0
        snapshot = self._power_snapshot_provider()
        thermal_state = getattr(snapshot, "thermal_state", None)
        low_power_mode = getattr(snapshot, "low_power_mode", None)
        wake_generation = getattr(snapshot, "wake_generation", None)
        if (
            thermal_state not in {"nominal", "fair", "serious", "critical", "unknown"}
            or type(low_power_mode) is not bool
            or type(wake_generation) is not int
            or wake_generation < 0
        ):
            raise ValueError("电源状态快照无效")
        if thermal_state in {"serious", "critical"}:
            self._raise_paused(
                request_kind,
                "系统温度压力较高，任务已暂停",
                "thermal_pressure",
            )
        if self._last_wake_generation is None:
            self._last_wake_generation = wake_generation
        elif wake_generation != self._last_wake_generation:
            if wake_generation < self._last_wake_generation:
                raise ValueError("唤醒代次不得倒退")
            if self._wake_probe is not None:
                try:
                    self._wake_probe()
                except OperationPaused:
                    self._emit_status("paused", request_kind, None)
                    raise
            self._last_wake_generation = wake_generation
        return 1.5 if thermal_state == "fair" or low_power_mode else 1.0

    def _emit_status(
        self,
        state: PacingState,
        request_kind: RequestKind | None,
        next_wait_seconds: float | None,
    ) -> None:
        if self._status_callback is None:
            return
        if next_wait_seconds is not None:
            self._require_finite(next_wait_seconds, field="下次等待时长")
        if self._mode == "standard":
            target_min = None
            target_max = None
        else:
            target_min, target_max = TARGET_WINDOWS_SECONDS[self._mode]
        self._status_callback(
            PacingStatus(
                mode=self._mode,
                state=state,
                request_kind=request_kind,
                next_wait_seconds=next_wait_seconds,
                target_min_seconds=target_min,
                target_max_seconds=target_max,
                disclaimer=_DISCLAIMER,
            )
        )

    def _read_monotonic(self) -> float:
        value = self._monotonic()
        self._require_finite(value, field="单调时钟值")
        return float(value)

    def _random_value(self, low: float, high: float) -> float:
        value = self._uniform(low, high)
        self._require_finite(value, field="随机值")
        numeric = float(value)
        if numeric < low or numeric > high:
            raise ValueError(f"随机值必须位于 {low} 至 {high} 之间")
        return numeric

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("请求节奏调度器已关闭")

    @staticmethod
    def _validate_mode(mode: object) -> PacingMode:
        if not isinstance(mode, str):
            raise TypeError("mode 必须是字符串")
        if mode not in _VALID_MODES:
            raise ValueError(f"未知请求节奏档位：{mode}")
        return mode  # type: ignore[return-value]

    @staticmethod
    def _validate_request_kind(kind: object) -> RequestKind:
        if not isinstance(kind, str):
            raise TypeError("kind 必须是字符串")
        if kind not in _VALID_REQUEST_KINDS:
            raise ValueError(f"未知请求类型：{kind}")
        return kind  # type: ignore[return-value]

    @staticmethod
    def _validate_nonnegative_integer(value: object, *, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{field} 必须是非负整数")
        if value < 0:
            raise ValueError(f"{field} 必须是非负整数")

    @staticmethod
    def _require_finite(value: object, *, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{field}必须是有限数值")
        if not math.isfinite(value):
            raise ValueError(f"{field}必须是有限数值")
