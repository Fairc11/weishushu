from __future__ import annotations

import math
import threading

import pytest

from weibo_book.errors import (
    OperationCancelled,
    OperationPaused,
    WeiboError,
    WeiboErrorKind,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def make_scheduler(mode="low_2_3_hours", *, uniform=None, status_callback=None):
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    clock = FakeClock()
    scheduler = AdaptiveRequestScheduler(
        mode,
        monotonic=clock.monotonic,
        uniform=uniform or (lambda low, high: (low + high) / 2),
        wait=clock.wait,
        status_callback=status_callback,
    )
    return scheduler, clock


def test_modes_and_target_windows_are_exact():
    from weibo_book.archive.pacing import REQUEST_WEIGHTS, TARGET_WINDOWS_SECONDS

    assert TARGET_WINDOWS_SECONDS == {
        "low_2_3_hours": (7200, 10800),
        "low_4_6_hours": (14400, 21600),
        "low_8_12_hours": (28800, 43200),
    }
    assert REQUEST_WEIGHTS == {
        "profile": 2,
        "detail": 1,
        "comments": 2,
        "media": 1,
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("low_2_3_hours", 60.0),
        ("low_4_6_hours", 120.0),
        ("low_8_12_hours", 240.0),
    ],
)
def test_known_requests_use_target_midpoint_without_real_hour_wait(mode, expected):
    scheduler, clock = make_scheduler(mode)
    scheduler.set_known_remaining(posts=50)

    scheduler.before_request("detail")

    assert sum(clock.waits) == pytest.approx(expected)
    assert clock.waits
    assert max(clock.waits) <= 0.25


@pytest.mark.parametrize(
    ("factor", "expected"),
    [(0.9, 108.0), (1.1, 132.0)],
)
def test_jitter_uses_inclusive_point_nine_to_one_point_one_bounds(factor, expected):
    requested_bounds = []

    def uniform(low, high):
        requested_bounds.append((low, high))
        return factor

    scheduler, clock = make_scheduler(uniform=uniform)
    scheduler.set_known_remaining(posts=50)

    scheduler.before_request("comments")

    assert requested_bounds == [(0.9, 1.1)]
    assert sum(clock.waits) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("profile", 120.0),
        ("comments", 120.0),
        ("detail", 60.0),
        ("media", 60.0),
    ],
)
def test_request_kinds_use_two_confirmed_weight_levels(kind, expected):
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=50)

    scheduler.before_request(kind)

    assert sum(clock.waits) == pytest.approx(expected)


def test_media_requests_recalculate_from_elapsed_time_without_resetting_clock():
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=1)
    scheduler.before_request("detail")
    assert clock.now == pytest.approx(3000.0)

    scheduler.add_media_requests(2)
    scheduler.before_request("media")

    # 已消耗 3000 秒，详情、评论和两个媒体的剩余权重合计为 5。
    assert clock.now == pytest.approx(4200.0)


def test_successful_request_reduces_known_remaining_count():
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=1)

    assert scheduler.run("detail", lambda: "ok") == "ok"
    first = clock.now
    assert scheduler.run("comments", lambda: "ok") == "ok"

    assert first == pytest.approx(3000.0)
    assert clock.now == pytest.approx(9000.0)


def test_dynamic_media_weight_preserves_total_target_midpoint():
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=1)

    assert scheduler.run("detail", lambda: "ok") == "ok"
    scheduler.add_media_requests(1)
    assert scheduler.run("comments", lambda: "ok") == "ok"
    assert scheduler.run("media", lambda: "ok") == "ok"

    assert clock.now == pytest.approx(9000.0)


def test_unknown_profile_estimation_wait_is_interruptible_one_to_three_seconds():
    requested_bounds = []

    def uniform(low, high):
        requested_bounds.append((low, high))
        return 2.0

    scheduler, clock = make_scheduler(uniform=uniform)

    scheduler.before_request("profile")

    assert requested_bounds == [(1.0, 3.0)]
    assert sum(clock.waits) == pytest.approx(2.0)
    assert max(clock.waits) <= 0.25


def test_standard_mode_waits_zero_and_preserves_exception_identity():
    scheduler, clock = make_scheduler("standard")
    original = WeiboError("接口失败", kind=WeiboErrorKind.API)

    with pytest.raises(WeiboError) as raised:
        scheduler.run("profile", lambda: (_ for _ in ()).throw(original))

    assert raised.value is original
    assert clock.waits == []


def test_standard_mode_does_not_call_uniform_or_wait():
    calls = []

    def forbidden(*_args):
        calls.append(True)
        raise AssertionError("标准模式不得调用随机源或等待函数")

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "standard",
        uniform=forbidden,
        wait=forbidden,
    )

    scheduler.before_request("profile")
    assert scheduler.run("detail", lambda: "ok") == "ok"
    assert calls == []


def test_low_mode_network_error_waits_once_and_retries_only_once():
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=1)
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(OperationPaused) as raised:
        scheduler.run("detail", fail)

    assert calls == 2
    assert raised.value.pause_reason == "network_unavailable"
    # 正常等待 3000 秒，唯一一次项目层延长等待为 3300 秒。
    assert clock.now == pytest.approx(6300.0)


def test_network_retry_wait_is_strictly_longer_and_reported_in_status():
    statuses = []
    scheduler, _clock = make_scheduler(status_callback=statuses.append)
    scheduler.set_known_remaining(posts=1)

    with pytest.raises(OperationPaused):
        scheduler.run(
            "detail",
            lambda: (_ for _ in ()).throw(
                WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)
            ),
        )

    waits = [
        status.next_wait_seconds
        for status in statuses
        if status.state == "waiting"
    ]
    assert waits == pytest.approx([3000.0, 3300.0])
    assert waits[1] > waits[0]


def test_network_retry_waits_one_second_after_target_is_exhausted():
    statuses = []
    scheduler, clock = make_scheduler(status_callback=statuses.append)
    scheduler.set_known_remaining(posts=1)
    clock.now = 9001.0
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(OperationPaused) as raised:
        scheduler.run("detail", fail)

    assert calls == 2
    assert raised.value.pause_reason == "network_unavailable"
    assert clock.now == pytest.approx(9002.0)
    waits = [
        status.next_wait_seconds
        for status in statuses
        if status.state == "waiting"
    ]
    assert waits == pytest.approx([0.0, 1.0])


def test_network_retry_success_counts_only_the_successful_request_once():
    scheduler, clock = make_scheduler()
    scheduler.set_known_remaining(posts=2)
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)
        return "ok"

    assert scheduler.run("detail", fail_once) == "ok"
    assert clock.now == pytest.approx(3150.0)

    # 详情成功后剩余权重为 5；不得把失败尝试或 before_request 重复扣减。
    assert scheduler.run("comments", lambda: "ok") == "ok"
    assert clock.now == pytest.approx(5490.0)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (WeiboErrorKind.AUTH, "authentication_required"),
        (WeiboErrorKind.RATE_LIMIT, "rate_limited"),
    ],
)
def test_auth_and_rate_limit_pause_without_retry(kind, reason):
    scheduler, _clock = make_scheduler()
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise WeiboError("停止请求", kind=kind)

    with pytest.raises(OperationPaused) as raised:
        scheduler.run("profile", fail)

    assert calls == 1
    assert raised.value.pause_reason == reason


@pytest.mark.parametrize("kind", [WeiboErrorKind.API, WeiboErrorKind.UNKNOWN])
def test_other_low_mode_errors_are_raised_unchanged(kind):
    scheduler, _clock = make_scheduler()
    original = WeiboError("原始错误", kind=kind, recoverable=True)

    with pytest.raises(WeiboError) as raised:
        scheduler.run("profile", lambda: (_ for _ in ()).throw(original))

    assert raised.value is original


def test_pause_interrupts_wait_within_quarter_second():
    pause_event = threading.Event()
    clock = FakeClock()
    statuses = []

    def wait(seconds):
        clock.wait(seconds)
        pause_event.set()

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        pause_event=pause_event,
        monotonic=clock.monotonic,
        uniform=lambda _low, _high: 1.0,
        wait=wait,
        status_callback=statuses.append,
    )
    scheduler.set_known_remaining(posts=1)

    with pytest.raises(OperationPaused) as raised:
        scheduler.before_request("detail")

    assert raised.value.pause_reason == "user_requested"
    assert clock.now <= 0.25
    assert statuses[-1].state == "paused"
    assert statuses[-1].request_kind == "detail"
    assert statuses[-1].next_wait_seconds is None


def test_pause_during_profile_estimation_reports_paused_before_raising():
    pause_event = threading.Event()
    clock = FakeClock()
    statuses = []

    def wait(seconds):
        clock.wait(seconds)
        pause_event.set()

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        pause_event=pause_event,
        monotonic=clock.monotonic,
        uniform=lambda _low, _high: 2.0,
        wait=wait,
        status_callback=statuses.append,
    )

    with pytest.raises(OperationPaused) as raised:
        scheduler.before_request("profile")

    assert raised.value.pause_reason == "user_requested"
    assert [status.state for status in statuses] == ["estimating", "paused"]
    assert statuses[-1].request_kind == "profile"
    assert statuses[-1].next_wait_seconds is None


def test_cancel_interrupts_wait_within_quarter_second():
    cancel_event = threading.Event()
    clock = FakeClock()
    statuses = []

    def wait(seconds):
        clock.wait(seconds)
        cancel_event.set()

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        cancel_event=cancel_event,
        monotonic=clock.monotonic,
        uniform=lambda _low, _high: 1.0,
        wait=wait,
        status_callback=statuses.append,
    )
    scheduler.set_known_remaining(posts=1)

    with pytest.raises(OperationCancelled):
        scheduler.before_request("detail")

    assert clock.now <= 0.25
    assert all(status.state != "paused" for status in statuses)


def test_pause_set_by_requesting_callback_prevents_initial_operation():
    pause_event = threading.Event()
    operations = 0

    def on_status(status):
        if status.state == "requesting":
            pause_event.set()

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        pause_event=pause_event,
        monotonic=lambda: 0.0,
        uniform=lambda _low, _high: 1.0,
        wait=lambda _seconds: None,
        status_callback=on_status,
    )
    scheduler.set_known_remaining(posts=1)

    def operation():
        nonlocal operations
        operations += 1
        return "ok"

    with pytest.raises(OperationPaused):
        scheduler.run("detail", operation)

    assert operations == 0


def test_cancel_set_by_retry_requesting_callback_prevents_retry_operation():
    cancel_event = threading.Event()
    requesting_count = 0
    operations = 0

    def on_status(status):
        nonlocal requesting_count
        if status.state == "requesting":
            requesting_count += 1
            if requesting_count == 2:
                cancel_event.set()

    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        cancel_event=cancel_event,
        monotonic=lambda: 0.0,
        uniform=lambda _low, _high: 1.0,
        wait=lambda _seconds: None,
        status_callback=on_status,
    )
    scheduler.set_known_remaining(posts=1)

    def operation():
        nonlocal operations
        operations += 1
        raise WeiboError("网络中断", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(OperationCancelled):
        scheduler.run("detail", operation)

    assert operations == 1


def test_status_callback_receives_frozen_explanatory_statuses():
    from dataclasses import FrozenInstanceError

    statuses = []
    scheduler, _clock = make_scheduler(status_callback=statuses.append)
    scheduler.set_known_remaining(posts=1)
    scheduler.run("detail", lambda: "ok")

    waiting = next(status for status in statuses if status.state == "waiting")
    requesting = next(status for status in statuses if status.state == "requesting")
    assert waiting.mode == "low_2_3_hours"
    assert waiting.request_kind == "detail"
    assert waiting.next_wait_seconds == pytest.approx(3000.0)
    assert waiting.target_min_seconds == 7200
    assert waiting.target_max_seconds == 10800
    assert waiting.disclaimer == "目标区间不是完成时间承诺"
    assert requesting.next_wait_seconds is None
    with pytest.raises(FrozenInstanceError):
        waiting.state = "paused"


@pytest.mark.parametrize("invalid", ["slow", "LOW_2_3_HOURS", None, True])
def test_unknown_mode_is_rejected(invalid):
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    with pytest.raises((TypeError, ValueError)):
        AdaptiveRequestScheduler(invalid)


@pytest.mark.parametrize("invalid", ["post", "PROFILE", None, True])
def test_unknown_request_kind_is_rejected(invalid):
    scheduler, _clock = make_scheduler()

    with pytest.raises((TypeError, ValueError)):
        scheduler.before_request(invalid)


@pytest.mark.parametrize("invalid", [-1, True, 1.5, float("nan"), float("inf")])
def test_remaining_post_count_requires_nonnegative_strict_integer(invalid):
    scheduler, _clock = make_scheduler()

    with pytest.raises((TypeError, ValueError)):
        scheduler.set_known_remaining(posts=invalid)


@pytest.mark.parametrize("invalid", [-1, True, 1.5, float("nan"), float("inf")])
def test_media_count_requires_nonnegative_strict_integer(invalid):
    scheduler, _clock = make_scheduler()

    with pytest.raises((TypeError, ValueError)):
        scheduler.add_media_requests(invalid)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_injected_random_value_is_rejected(invalid):
    scheduler, _clock = make_scheduler(uniform=lambda _low, _high: invalid)
    scheduler.set_known_remaining(posts=1)

    with pytest.raises(ValueError, match="有限"):
        scheduler.before_request("detail")


def test_close_rejects_every_public_operation():
    scheduler, _clock = make_scheduler()
    scheduler.close()
    scheduler.close()

    with pytest.raises(RuntimeError, match="已关闭"):
        scheduler.set_known_remaining(posts=1)
    with pytest.raises(RuntimeError, match="已关闭"):
        scheduler.add_media_requests(1)
    with pytest.raises(RuntimeError, match="已关闭"):
        scheduler.before_request("detail")
    with pytest.raises(RuntimeError, match="已关闭"):
        scheduler.run("detail", lambda: None)


def test_nonfinite_monotonic_value_is_rejected():
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        monotonic=lambda: math.nan,
        uniform=lambda _low, _high: 1.0,
        wait=lambda _seconds: None,
    )

    with pytest.raises(ValueError, match="有限"):
        scheduler.set_known_remaining(posts=1)
