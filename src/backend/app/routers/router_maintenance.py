"""设置面板的维护端点：系统通知、缓存统计与清理。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.services import maintenance
from weibo_book.errors import WeiboError

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


class NotifyRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=300)


class CacheClearRequest(BaseModel):
    key: str = Field(min_length=1, max_length=32)


@router.post("/notify")
async def api_notify(request: NotifyRequest):
    return await asyncio.to_thread(
        maintenance.send_notification, request.title, request.message
    )


@router.get("/cache")
async def api_cache_items():
    return {"items": await asyncio.to_thread(maintenance.cache_items)}


@router.post("/cache/clear")
async def api_cache_clear(request: CacheClearRequest):
    try:
        return await asyncio.to_thread(maintenance.clear_cache_item, request.key)
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
