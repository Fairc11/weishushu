"""WebSocket：客户端订阅任务进度。

协议（plan §2.3）：
  snapshot | progress | log | done | error | cancelled

服务端用 asyncio.Queue 缓冲，客户端断开自动 unregister。
注意：WS 路径不能加 /api 前缀（mount 顺序在 main.py 单独处理）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.app.services import log_handler
from backend.app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str) -> None:
    await websocket.accept()
    rec = task_manager.get(task_id)
    if rec is None:
        await websocket.send_json({"type": "error", "error": f"task {task_id} not found"})
        await websocket.close()
        return

    q = await task_manager.subscribe(task_id)
    if q is None:
        await websocket.close()
        return

    # 同时拉 log 流（按 1s 节流）
    last_log_ts: float | None = None

    try:
        while True:
            # 等待任务消息（带超时，便于定期发 log 快照 + 探活）
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1.0)
                await websocket.send_json(msg)
                if msg.get("type") in ("waiting_resume", "done", "error", "cancelled", "abandoned"):
                    break
            except asyncio.TimeoutError:
                pass

            # 推一波新日志
            entries = log_handler.tail(50, since_ts=last_log_ts)
            if entries:
                last_log_ts = entries[-1]["ts"]
                await websocket.send_json({"type": "log", "entries": entries})
    except WebSocketDisconnect:
        logger.info("WS 客户端断开: task=%s", task_id)
    except Exception as e:
        logger.exception("WS 异常: %s", e)
    finally:
        await task_manager.unsubscribe(task_id, q)
