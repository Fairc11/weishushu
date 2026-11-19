"""本地性能计时；只记录阶段名称与秒数，不保留业务内容。"""
from contextlib import contextmanager
import threading
import time


class PerformanceTimings:
    def __init__(self, monotonic=time.monotonic):
        self._clock = monotonic
        self._totals = {}
        self._active = {}
        self._lock = threading.RLock()

    @contextmanager
    def measure(self, stage):
        token = object()
        with self._lock:
            self._active[token] = (stage, self._clock())
        try:
            yield
        finally:
            with self._lock:
                key, started = self._active.pop(token)
                self._totals[key] = self._totals.get(key, 0.0) + max(0.0, self._clock() - started)

    def snapshot(self):
        with self._lock:
            result = dict(self._totals)
            now = self._clock()
            for stage, started in self._active.values():
                result[stage] = result.get(stage, 0.0) + max(0.0, now - started)
            return result
