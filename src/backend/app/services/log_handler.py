"""自定义 logging handler → 内存环形缓冲，logs router 从这里拉。

为什么不用 Queue：log 写入是热路径，Queue + asyncio.put 可能阻塞业务线程。
环形 buffer + threading.Lock 是最稳的（参考 Python logging 库本身设计）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

_MAX_ENTRIES = 2000  # 内存上限，防泄漏


class RingBufferHandler(logging.Handler):
    """线程安全的环形缓冲，固定大小。"""

    def __init__(self, capacity: int = _MAX_ENTRIES) -> None:
        super().__init__()
        self._buf: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "msg": self.format(record),
            }
            with self._lock:
                self._buf.append(entry)
        except Exception:
            # logging 自身的 emit 不能再炸
            self.handleError(record)

    def tail(self, n: int = 200, since_ts: Optional[float] = None) -> list[dict]:
        with self._lock:
            data = list(self._buf)
        if since_ts is not None:
            data = [e for e in data if e["ts"] >= since_ts]
        return data[-n:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


# 进程级单例
_buffer = RingBufferHandler()
_buffer.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                       datefmt="%H:%M:%S"))


def install(level: int = logging.INFO) -> None:
    """挂在 root logger，确保所有业务日志都进 buffer。"""
    root = logging.getLogger()
    if _buffer not in root.handlers:
        root.addHandler(_buffer)
    root.setLevel(level)


def tail(n: int = 200, since_ts: Optional[float] = None) -> list[dict]:
    return _buffer.tail(n, since_ts)


def clear() -> None:
    _buffer.clear()
