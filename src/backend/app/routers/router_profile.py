"""URL 解析 + 用户名片（步骤 3「URL 确认」前置）。

设计取舍：profile.resolve 不创建任务（同步、轻量、< 1s），直接返回用户信息。
预览用 router_scraper 的 preview 端点（也是同步）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from backend.app.features import raise_future_feature
from backend.app.schemas import ResolveURLResponse
from backend.app.services.rate_limit import check_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _user_to_dict(user) -> dict:
    return {
        "uid": user.uid,
        "screen_name": user.screen_name,
        "avatar_url": user.avatar_url,
        "description": user.description,
        "followers_count": user.followers_count,
        "following_count": user.following_count,
        "posts_count": user.posts_count,
        "verified": user.verified,
        "verified_reason": user.verified_reason,
        "location": user.location,
        "gender": user.gender,
        "cover_image_url": user.cover_image_url,
    }


@router.post("/resolve", response_model=ResolveURLResponse)
async def resolve_url(request: Request) -> ResolveURLResponse:
    """任意主页解析保留路由，当前版本不进入限流或业务核心。"""
    raise_future_feature()
