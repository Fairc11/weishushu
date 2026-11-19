"""v2.1.0 检查更新 API。

端点：
- GET  /api/update/check   比对公开仓库最新 Release 与当前版本
- POST /api/update/install 下载最新 DMG、校验 sha256 并暂存（仅 frozen macOS 正式版）
- POST /api/update/apply   启动退出后替换脚本；前端随后关闭窗口完成更新

详细业务：backend.app.services.update_check
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.app.services import update_check
from weibo_book.errors import WeiboError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/update", tags=["update"])


class ApplyRequest(BaseModel):
    staged_path: str


@router.get("/check")
async def api_update_check():
    try:
        return await asyncio.to_thread(update_check.check_latest_release)
    except WeiboError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/install")
async def api_update_install():
    if not update_check.can_auto_install():
        raise HTTPException(
            status_code=400,
            detail="只有 macOS 正式安装版支持自动更新；请前往发布页手动下载",
        )
    try:
        return await asyncio.to_thread(update_check.stage_latest_release)
    except WeiboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/apply")
async def api_update_apply(request: ApplyRequest):
    try:
        return await asyncio.to_thread(
            update_check.apply_staged_update, request.staged_path
        )
    except WeiboError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
