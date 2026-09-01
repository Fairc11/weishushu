"""进程级共享资源：httpx 客户端 + 任务管理器（阶段 2 用，阶段 1 先占位）。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


class SharedHttpClient:
    """FastAPI lifespan 钩子创建/关闭，避免每次请求新建。"""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def start(self, timeout: float = 30.0) -> None:
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 Weishushu/1.1.1"},
                )
                logger.info("SharedHttpClient started")

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
                logger.info("SharedHttpClient closed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SharedHttpClient not started (lifespan 没跑?)")
        return self._client


shared_http = SharedHttpClient()


@asynccontextmanager
async def lifespan_context(app) -> AsyncIterator[None]:
    """FastAPI lifespan 钩子的工厂。让 main.py 只写一行。

    v1.2.0 P0 (B-04)：finally 兜底清空 task_manager._tasks，防进程退出时残留。
    """
    await shared_http.start()
    # v1.1.3 D1+D3：启动时检测 Playwright Chromium + WebView2
    from backend.app.services import setup_check
    setup_check.install()
    from backend.app.services.task_manager import task_manager
    persistent = await task_manager.reconcile_after_process_start()
    if persistent is not None and persistent.state == "cancelling":
        if persistent.task_kind == "following_archive":
            from backend.app.services.following_archive_tasks import following_archive_tasks
            await following_archive_tasks.finish_interrupted_cancel(persistent)
        else:
            from backend.app.services.personal_archive_tasks import personal_archive_tasks
            await personal_archive_tasks.finish_interrupted_cancel(persistent)
    try:
        yield
    finally:
        await shared_http.close()
        # B-04 兜底：进程退出时清掉所有任务记录 + 取消 GC timer
        for handle in list(task_manager._gc_timers.values()):
            try:
                handle.cancel()
            except Exception:
                pass
        task_manager._gc_timers.clear()
        task_manager._tasks.clear()
        logger.info("task_manager cleared on shutdown")
