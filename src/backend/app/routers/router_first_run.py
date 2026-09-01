"""v2.0.0 首启风险须知 API。

端点：
- POST /api/first-run/check   返 {accepted: bool}（前端 init() 调一次）
- POST /api/first-run/accept  写标记文件，返 {accepted: true, marker_path}

详细业务：backend.app.services.first_run
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.app.services.first_run import accept, is_accepted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/first-run", tags=["first-run"])


class CheckResponse(BaseModel):
    accepted: bool


class AcceptResponse(BaseModel):
    accepted: bool
    marker_path: str


@router.post("/check", response_model=CheckResponse)
async def first_run_check() -> CheckResponse:
    """检查 v2.0.0 风险须知是否已接受。"""
    return CheckResponse(accepted=is_accepted())


@router.post("/accept", response_model=AcceptResponse)
async def first_run_accept() -> AcceptResponse:
    """标记 v2.0.0 风险须知已接受（写标记文件）。"""
    p = accept()
    return AcceptResponse(accepted=True, marker_path=str(p))
