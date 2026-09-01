"""macOS 电源、热状态、睡眠唤醒和任务活动声明。"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Literal

logger = logging.getLogger(__name__)

ThermalState = Literal["nominal", "fair", "serious", "critical", "unknown"]


@dataclass(frozen=True)
class PowerSnapshot:
    external_connected: bool | None
    low_power_mode: bool
    thermal_state: ThermalState
    wake_generation: int


class SystemPowerService:
    """只在 macOS 上读取已确认的系统接口；检测失败保持未知。"""

    def __init__(
        self,
        *,
        platform: str = sys.platform,
        run_command: Callable = subprocess.run,
        process_info_factory: Callable | None = None,
        activity_options: int | None = None,
    ) -> None:
        self._platform = platform
        self._run_command = run_command
        self._process_info_factory = process_info_factory
        self._activity_options = activity_options
        self._lock = threading.RLock()
        self._wake_generation = 0
        self._notification_center = None
        self._observer = None

    def _process_info(self):
        if self._platform != "darwin":
            return None
        if self._process_info_factory is not None:
            return self._process_info_factory()
        from Foundation import NSProcessInfo

        return NSProcessInfo.processInfo()

    def _external_connected(self) -> bool | None:
        if self._platform != "darwin":
            return None
        try:
            completed = self._run_command(
                [
                    "/usr/sbin/ioreg",
                    "-a",
                    "-r",
                    "-c",
                    "AppleSmartBattery",
                    "-d",
                    "1",
                ],
                capture_output=True,
                check=True,
            )
            payload = plistlib.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
            logger.debug("读取 macOS 外接电源状态失败: %s", exc)
            return None
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            return None
        value = payload[0].get("ExternalConnected")
        return value if type(value) is bool else None

    def snapshot(self) -> PowerSnapshot:
        thermal_state: ThermalState = "unknown"
        low_power_mode = False
        try:
            process_info = self._process_info()
            if process_info is not None:
                thermal_state = {
                    0: "nominal",
                    1: "fair",
                    2: "serious",
                    3: "critical",
                }.get(int(process_info.thermalState()), "unknown")
                low_power_mode = bool(process_info.isLowPowerModeEnabled())
        except Exception as exc:
            logger.debug("读取 macOS 热状态或低电量模式失败: %s", exc)
        with self._lock:
            wake_generation = self._wake_generation
        return PowerSnapshot(
            external_connected=self._external_connected(),
            low_power_mode=low_power_mode,
            thermal_state=thermal_state,
            wake_generation=wake_generation,
        )

    def begin_keep_awake(self, reason: str) -> object | None:
        if self.snapshot().external_connected is not True:
            return None
        try:
            process_info = self._process_info()
            if process_info is None:
                return None
            options = self._activity_options
            if options is None:
                from Foundation import NSActivityIdleSystemSleepDisabled, NSActivityUserInitiated

                options = int(NSActivityUserInitiated) | int(NSActivityIdleSystemSleepDisabled)
            return process_info.beginActivityWithOptions_reason_(options, reason)
        except Exception as exc:
            logger.warning("开始 macOS 清醒活动失败: %s", exc)
            return None

    def end_keep_awake(self, token: object | None) -> None:
        if token is None:
            return
        try:
            process_info = self._process_info()
            if process_info is not None:
                process_info.endActivity_(token)
        except Exception as exc:
            logger.warning("结束 macOS 清醒活动失败: %s", exc)

    def start_observing(self) -> None:
        if self._platform != "darwin" or self._observer is not None:
            return
        try:
            from AppKit import (
                NSWorkspace,
                NSWorkspaceDidWakeNotification,
                NSWorkspaceWillSleepNotification,
            )
            from Foundation import NSObject

            service = self

            class _WorkspaceObserver(NSObject):
                def willSleep_(self, _notification):
                    service._record_sleep()

                def didWake_(self, _notification):
                    service._record_wake()

            observer = _WorkspaceObserver.alloc().init()
            center = NSWorkspace.sharedWorkspace().notificationCenter()
            center.addObserver_selector_name_object_(
                observer,
                "willSleep:",
                NSWorkspaceWillSleepNotification,
                None,
            )
            center.addObserver_selector_name_object_(
                observer,
                "didWake:",
                NSWorkspaceDidWakeNotification,
                None,
            )
            self._observer = observer
            self._notification_center = center
        except Exception as exc:
            logger.warning("注册 macOS 睡眠唤醒观察器失败: %s", exc)

    def stop_observing(self) -> None:
        observer = self._observer
        center = self._notification_center
        self._observer = None
        self._notification_center = None
        if observer is None or center is None:
            return
        try:
            center.removeObserver_(observer)
        except Exception as exc:
            logger.warning("移除 macOS 睡眠唤醒观察器失败: %s", exc)

    def _record_sleep(self) -> None:
        logger.info("macOS 即将睡眠，低强度任务等待唤醒后复检")

    def _record_wake(self) -> None:
        with self._lock:
            self._wake_generation += 1
        logger.info("macOS 已唤醒，下一次低强度请求前将复检会话")


class KeepAwakeLease:
    """把用户选择、电源变化和活动 token 的释放绑定在一起。"""

    def __init__(
        self,
        service: SystemPowerService,
        *,
        enabled: bool,
        reason: str,
        monitor_interval: float = 5.0,
    ) -> None:
        self._service = service
        self._enabled = enabled
        self._reason = reason
        self._monitor_interval = monitor_interval
        self._token: object | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def refresh(self, snapshot: PowerSnapshot | None = None) -> None:
        if not self._enabled:
            return
        current = snapshot or self._service.snapshot()
        with self._lock:
            if self._closed:
                return
            if current.external_connected is True:
                if self._token is None:
                    self._token = self._service.begin_keep_awake(self._reason)
                return
            if self._token is not None:
                self._service.end_keep_awake(self._token)
                self._token = None

    def start_monitoring(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._closed or self._monitor_thread is not None:
                return

            def monitor() -> None:
                while not self._monitor_stop.wait(self._monitor_interval):
                    self.refresh()

            self._monitor_thread = threading.Thread(
                target=monitor,
                name="weishushu-power-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def close(self) -> None:
        self._monitor_stop.set()
        with self._lock:
            self._closed = True
            token = self._token
            self._token = None
            monitor = self._monitor_thread
            self._monitor_thread = None
        if token is not None:
            self._service.end_keep_awake(token)
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=1.0)


system_power_service = SystemPowerService()
