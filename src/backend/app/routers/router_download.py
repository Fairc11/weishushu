"""独立媒体下载。

v1.2.0 P0 安全整改 (B-01 + B-08)：
- 原端点 `/api/download/media` 是开放 SSRF：任意 URL 服务端拉取 + 任意写盘
- 媒体下载已合并进 `book.generate(..., download_media=True)` 路径，单独端点冗余
- 修法：返回 410 Gone + 注释说明调用方应改用 `/api/scrape/start` 带 `download_media=True`
- 保留 router 与 import 兼容（main.py 还在 include_router），但功能下线
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/download", tags=["download"])


@router.post("/media")
async def download_media() -> dict:
    """v1.2.0 安全下线（B-01）：该端点存在开放 SSRF + 任意写盘风险，已被业务核心的
    `book.generate(download_media=True)` 路径覆盖。前端若需下载媒体，请调
    `/api/scrape/start` 时把 `download_media` 设为 `true`，由 generate 统一调度。
    """
    raise HTTPException(
        status_code=410,
        detail="v1.2.1 已删除 /api/download/media（开放 SSRF + 写盘风险）。请用 /api/scrape/start 带 download_media=True，由 generate 统一调度下载。",
    )
