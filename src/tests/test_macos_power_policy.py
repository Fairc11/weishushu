from __future__ import annotations

import plistlib
import threading

import pytest

from weibo_book.errors import OperationPaused, WeiboError, WeiboErrorKind


class FakeProcessInfo:
    def __init__(self, *, thermal=0, low_power=False):
        self.thermal = thermal
        self.low_power = low_power
        self.begun = []
        self.ended = []

    def thermalState(self):
        return self.thermal

    def isLowPowerModeEnabled(self):
        return self.low_power

    def beginActivityWithOptions_reason_(self, options, reason):
        token = object()
        self.begun.append((options, reason, token))
        return token

    def endActivity_(self, token):
        self.ended.append(token)


def _ioreg(external):
    payload = [{}] if external is None else [{"ExternalConnected": external}]
    return type("Completed", (), {"stdout": plistlib.dumps(payload)})()


def test_power_snapshot_reads_only_exact_external_connected_boolean():
    from backend.app.services.system_power import SystemPowerService

    connected = SystemPowerService(
        platform="darwin",
        run_command=lambda *_args, **_kwargs: _ioreg(True),
        process_info_factory=lambda: FakeProcessInfo(thermal=1, low_power=True),
    ).snapshot()
    unknown = SystemPowerService(
        platform="darwin",
        run_command=lambda *_args, **_kwargs: _ioreg("Yes"),
        process_info_factory=lambda: FakeProcessInfo(),
    ).snapshot()

    assert connected.external_connected is True
    assert connected.thermal_state == "fair"
    assert connected.low_power_mode is True
    assert unknown.external_connected is None


def test_non_macos_or_detection_failure_never_assumes_external_power():
    from backend.app.services.system_power import SystemPowerService

    non_macos = SystemPowerService(platform="win32").snapshot()
    failed = SystemPowerService(
        platform="darwin",
        run_command=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("失败")),
        process_info_factory=lambda: FakeProcessInfo(),
    ).snapshot()

    assert non_macos.external_connected is None
    assert failed.external_connected is None


def test_keep_awake_requires_user_choice_and_external_power_then_releases_on_battery():
    from backend.app.services.system_power import KeepAwakeLease, SystemPowerService

    external = True
    process = FakeProcessInfo()
    service = SystemPowerService(
        platform="darwin",
        run_command=lambda *_args, **_kwargs: _ioreg(external),
        process_info_factory=lambda: process,
        activity_options=0x1234,
    )
    disabled = KeepAwakeLease(service, enabled=False, reason="低强度本人归档")
    enabled = KeepAwakeLease(service, enabled=True, reason="低强度本人归档")

    disabled.refresh()
    assert process.begun == []
    enabled.refresh()
    assert len(process.begun) == 1
    assert process.begun[0][:2] == (0x1234, "低强度本人归档")

    external = False
    enabled.refresh()
    assert process.ended == [process.begun[0][2]]

    external = True
    enabled.refresh()
    enabled.close()
    assert len(process.begun) == 2
    assert process.ended[-1] is process.begun[1][2]


def test_keep_awake_monitor_releases_during_render_when_power_changes():
    from backend.app.services.system_power import KeepAwakeLease, SystemPowerService

    external = True
    released = threading.Event()

    class Process(FakeProcessInfo):
        def endActivity_(self, token):
            super().endActivity_(token)
            released.set()

    process = Process()
    service = SystemPowerService(
        platform="darwin",
        run_command=lambda *_args, **_kwargs: _ioreg(external),
        process_info_factory=lambda: process,
        activity_options=0x1234,
    )
    lease = KeepAwakeLease(
        service,
        enabled=True,
        reason="低强度本人归档",
        monitor_interval=0.01,
    )
    lease.refresh()
    lease.start_monitoring()
    external = False

    assert released.wait(1.0)
    lease.close()
    assert process.ended == [process.begun[0][2]]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_phase", ["sync", "render"])
async def test_keep_awake_lease_covers_render_and_final_inspection(
    tmp_path,
    monkeypatch,
    initial_phase,
):
    from backend.app.schemas import ArchiveFolderInspection
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.task_manager import TaskManager
    from weibo_book.archive.sync import SyncResult

    leases = []

    class Lease:
        def __init__(self, _service, *, enabled, reason):
            assert enabled is True
            self.closed = False
            self.monitoring = False
            leases.append(self)

        def refresh(self, _snapshot=None):
            assert not self.closed

        def close(self):
            self.closed = True

        def start_monitoring(self):
            assert not self.closed
            self.monitoring = True

    monkeypatch.setattr(
        "backend.app.services.personal_archive_tasks.KeepAwakeLease",
        Lease,
    )
    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    root = (tmp_path / "book").resolve()
    task_id = await manager.create_personal_archive(
        mode="create",
        output_dir=str(root),
        expected_uid="10001",
        pacing_mode="low_2_3_hours",
        keep_awake_when_plugged=True,
    )
    record = manager.get(task_id)
    assert record is not None
    if initial_phase == "render":
        manager._persist(record, phase="render")
    inspections = 0

    def inspect(path, *, current_uid):
        nonlocal inspections
        inspections += 1
        if inspections == 1 and initial_phase == "sync":
            return ArchiveFolderInspection(state="empty", path=path)
        assert leases and not leases[0].closed
        return ArchiveFolderInspection(
            state="archive", path=path, uid=current_uid, total_posts=0
        )

    class Sync:
        def run(self, mode):
            return SyncResult(mode, 0, 0, 0, 0, [])

    def render(*_args, **_kwargs):
        assert leases and not leases[0].closed
        assert leases[0].monitoring
        return []

    service = PersonalArchiveTaskService(
        manager=manager,
        dependency_builder=lambda _uid: (object(), None),
        sync_factory=lambda *_args, **_kwargs: Sync(),
        render_func=render,
        inspector=inspect,
        legacy_cleanup_func=lambda _output_dir: None,
    )
    started = service._launch(
        task_id, "create", str(root), "10001", "本人", resuming=False
    )
    await started.worker

    assert manager.snapshot(task_id)["state"] == "done"
    assert leases[0].closed is True


@pytest.mark.parametrize(
    ("thermal_state", "low_power_mode"),
    [("fair", False), ("nominal", True)],
)
def test_scheduler_multiplies_wait_for_fair_thermal_or_low_power(
    thermal_state,
    low_power_mode,
):
    from backend.app.services.system_power import PowerSnapshot
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    waits = []
    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        monotonic=lambda: sum(waits),
        uniform=lambda low, high: 1.0,
        wait=waits.append,
        power_snapshot_provider=lambda: PowerSnapshot(
            True, low_power_mode, thermal_state, 0
        ),
    )
    scheduler.set_known_remaining(posts=1)
    scheduler.before_request("detail")

    assert sum(waits) == pytest.approx(4500.0)


def test_sleep_only_records_state_and_wake_increments_generation():
    from backend.app.services.system_power import SystemPowerService

    service = SystemPowerService(platform="win32")
    service._record_sleep()
    assert service.snapshot().wake_generation == 0
    service._record_wake()
    assert service.snapshot().wake_generation == 1


@pytest.mark.parametrize("thermal_state", ["serious", "critical"])
def test_scheduler_pauses_before_request_under_thermal_pressure(thermal_state):
    from backend.app.services.system_power import PowerSnapshot
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        wait=lambda _seconds: None,
        power_snapshot_provider=lambda: PowerSnapshot(
            True, False, thermal_state, 0
        ),
    )

    with pytest.raises(OperationPaused) as raised:
        scheduler.before_request("profile")

    assert raised.value.pause_reason == "thermal_pressure"


def test_wake_generation_runs_session_probe_once_before_next_request():
    from backend.app.services.system_power import PowerSnapshot
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    generation = 0
    probes = []
    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        uniform=lambda low, high: 1.0,
        wait=lambda _seconds: None,
        power_snapshot_provider=lambda: PowerSnapshot(
            True, False, "nominal", generation
        ),
    )
    scheduler.set_wake_probe(lambda: probes.append("probe"))

    scheduler.before_request("profile")
    generation = 1
    scheduler.before_request("profile")
    scheduler.before_request("profile")

    assert probes == ["probe"]


def test_wake_after_provider_setup_but_before_first_request_still_probes():
    from backend.app.services.system_power import PowerSnapshot
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    generation = 0
    probes = []
    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        uniform=lambda low, high: 1.0,
        wait=lambda _seconds: None,
    )
    scheduler.set_power_snapshot_provider(
        lambda: PowerSnapshot(True, False, "nominal", generation)
    )
    scheduler.set_wake_probe(lambda: probes.append("probe"))
    generation = 1

    scheduler.before_request("profile")

    assert probes == ["probe"]


def test_network_retry_applies_new_fair_thermal_multiplier():
    from backend.app.services.system_power import PowerSnapshot
    from weibo_book.archive.pacing import AdaptiveRequestScheduler

    waits = []
    fair = False
    scheduler = AdaptiveRequestScheduler(
        "low_2_3_hours",
        monotonic=lambda: sum(waits),
        uniform=lambda low, high: 1.0,
        wait=waits.append,
    )
    scheduler.set_power_snapshot_provider(
        lambda: PowerSnapshot(True, False, "fair" if fair else "nominal", 0)
    )
    scheduler.set_known_remaining(posts=1)
    attempts = 0

    def request():
        nonlocal fair, attempts
        attempts += 1
        fair = True
        raise WeiboError("断网", kind=WeiboErrorKind.NETWORK)

    with pytest.raises(OperationPaused):
        scheduler.run("detail", request)

    assert attempts == 2
    assert sum(waits) == pytest.approx(7950.0)


def test_weibo_session_probe_uses_confirmed_config_fields_only():
    from weibo_book.archive.source import WeiboArchiveSource
    from weibo_book.models import ImageQuality

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"login": True, "uid": "10001"}}

    session = type(
        "Session",
        (),
        {"get": lambda self, url, timeout: (calls.append((url, timeout)) or Response())},
    )()
    calls = []
    extractor = type("Extractor", (), {"client": type("Client", (), {"session": session})()})()
    source = WeiboArchiveSource(
        extractor,
        self_uid="10001",
        image_quality=ImageQuality.ORIGINAL,
    )

    source.probe_session()

    assert calls == [("https://m.weibo.cn/api/config", 5)]


@pytest.mark.parametrize(
    ("payload", "pause_reason"),
    [
        ({"data": {"login": False, "uid": "10001"}}, "authentication_required"),
        ({"data": {"login": True, "uid": "20002"}}, "account_mismatch"),
    ],
)
def test_weibo_session_probe_pauses_for_login_or_uid_mismatch(payload, pause_reason):
    from weibo_book.archive.source import WeiboArchiveSource
    from weibo_book.models import ImageQuality

    response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: payload,
        },
    )()
    session = type("Session", (), {"get": lambda self, *_args, **_kwargs: response})()
    extractor = type("Extractor", (), {"client": type("Client", (), {"session": session})()})()
    source = WeiboArchiveSource(
        extractor,
        self_uid="10001",
        image_quality=ImageQuality.ORIGINAL,
    )

    with pytest.raises(OperationPaused) as raised:
        source.probe_session()

    assert raised.value.pause_reason == pause_reason


def test_weibo_session_probe_preserves_exact_432_pause_reason():
    from weibo_book.archive.source import WeiboArchiveSource
    from weibo_book.models import ImageQuality

    response = type("Response", (), {"status_code": 432})()
    session = type("Session", (), {"get": lambda self, *_args, **_kwargs: response})()
    extractor = type("Extractor", (), {"client": type("Client", (), {"session": session})()})()
    source = WeiboArchiveSource(
        extractor,
        self_uid="10001",
        image_quality=ImageQuality.ORIGINAL,
    )

    with pytest.raises(OperationPaused) as raised:
        source.probe_session()

    assert raised.value.pause_reason == "rate_limited"
