"""v1.1.3 U1：收藏面板独立 API。

v1.2.0 P0 (B-11)：
- 加 `check_profile` 限流（与 profile_limiter 一致：10/min/IP）
- url 走 Pydantic 校验（min_length + max_length）
- count 走 ge=0, le=500 上限
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.app.services.rate_limit import check_profile
from weibo_book.errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


class FavoritesListRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500, description="用户主页 URL")
    count: int = Field(20, ge=0, le=500, description="0=全部；实际 0-500")


@router.post("/list")
async def list_favorites(req: FavoritesListRequest, request: Request) -> dict:
    """独立收藏列表端点（v1.1.3 U1）：URL → 收藏前 N 条（不下载媒体，仅元数据）。"""
    check_profile(request)  # B-11: 10/min/IP
    try:
        from weibo_book import WeiboBook
        cookie_str = WeiboBook().ensure_login(force=False)
    except Exception as e:
        logger.warning("读取缓存 Cookie 失败，将使用匿名模式: %s", e)
        cookie_str = None

    try:
        from weibo_book.extractor import WeiboExtractor
        ext = WeiboExtractor(cookie_str=cookie_str)
        uid = ext.resolve_url(req.url)
        ext.get_user_info(uid)
        favorites = ext.get_favorites(uid, max_posts=req.count)

        return {
            "user": {
                "uid": ext._user_info.uid,
                "screen_name": ext._user_info.screen_name,
                "avatar_url": ext._user_info.avatar_url,
            },
            "favorites": [
                {
                    "bid": p.bid,
                    "text": (p.text or "")[:200],
                    "created_at": str(p.created_at) if p.created_at else "",
                    "media_count": len(p.media),
                    "cover_url": (p.media[0].thumbnail or p.media[0].url) if p.media else "",
                    "likes_count": p.likes_count,
                    "comments_count": p.comments_count,
                } for p in favorites
            ],
            "partial": ext._last_partial,
            "pages_total": ext._last_pages_total,
        }
    except WeiboError as we:
        status = 401 if we.kind == WeiboErrorKind.AUTH else 404 if we.kind == WeiboErrorKind.NOT_FOUND else 400
        raise HTTPException(status_code=status, detail=str(we))
    except Exception as e:
        logger.exception("list_favorites 失败: %s", e)
        raise HTTPException(status_code=400, detail=f"收藏列表失败: {e}")
