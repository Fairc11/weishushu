"""浮动日志拉取。前端右下角 log 面板用。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from backend.app.services import log_handler

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/")
async def tail_logs(
    tail: int = Query(200, ge=1, le=2000),
    since: float | None = Query(None, description="只返回 ts >= since 的条目"),
) -> dict:
    return {"entries": log_handler.tail(tail, since), "ts": time.time()}


@router.delete("/")
async def clear_logs() -> dict:
    log_handler.clear()
    return {"cleared": True}
