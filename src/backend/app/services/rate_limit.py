"""轻量级内存 rate limit（v1.1.1 S4 修复）。

不引 slowapi 外部依赖——用 dict + 时间窗口做"每 IP 每 N 秒最多 M 次"。

注意：frozen 单进程应用足够；分布式 / 多 worker 部署需要换 Redis 后端。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class InMemoryRateLimiter:
    """滑动窗口 rate limiter（每 IP 每 window_sec 秒最多 max_calls 次）。"""

    def __init__(self, max_calls: int, window_sec: int) -> None:
        self.max_calls = max_calls
        self.window_sec = window_sec
        self._buckets: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        """检查是否超限；超限抛 HTTPException(429)。"""
        now = time.time()
        with self._lock:
            bucket = self._buckets[key]
            cutoff = now - self.window_sec
            # 清理过期
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                retry_after = int(bucket[0] + self.window_sec - now) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"请求过于频繁，{retry_after}s 后重试",
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

    def reset(self, key: Optional[str] = None) -> None:
        """重置（测试用）。"""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# 三个分档限流器（按端点重量分）
scraper_limiter = InMemoryRateLimiter(max_calls=3, window_sec=60)   # 启 Playwright 拉数据
login_limiter = InMemoryRateLimiter(max_calls=2, window_sec=60)     # 扫码 / Chrome 导入
profile_limiter = InMemoryRateLimiter(max_calls=10, window_sec=60)  # URL 解析轻量


def _client_key(request: Request) -> str:
    """取客户端 IP；pywebview 桌面壳走 127.0.0.1。"""
    return request.client.host if request.client else "unknown"


def check_scraper(request: Request) -> None:
    scraper_limiter.check(_client_key(request))


def check_login(request: Request) -> None:
    login_limiter.check(_client_key(request))


def check_profile(request: Request) -> None:
    profile_limiter.check(_client_key(request))
