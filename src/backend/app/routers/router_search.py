"""博主搜索与目标识别路由（备份他人微博入口）。

- ``POST /api/search/users``：按昵称搜索博主，带 V 优先排序。
- ``POST /api/search/resolve``：粘贴链接/分享文本/纯 UID → 目标博主身份。

两者都只返回公开资料，不触碰账号数据；走 m.weibo.cn 移动接口
（crawl4weibo 主通道），与微博正文抓取同源。
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.app.services.rate_limit import check_profile
from weibo_book.errors import WeiboError, WeiboErrorKind, classify_error
from weibo_book.url_parser import parse_uid_from_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"])

_UID_RE = re.compile(r"^\d{5,20}$")
_URL_HINT_RE = re.compile(r"https?://|weibo\.com|weibo\.cn|t\.cn", re.IGNORECASE)


class SearchUsersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=100)


class ResolveTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000)


def _build_extractor():
    """有登录 Cookie 用登录态，没有就匿名（搜索接口弱登录可用）。"""
    from weibo_book import WeiboBook
    from weibo_book.extractor import WeiboExtractor

    book = WeiboBook()
    cookie_str = book.ensure_login(force=False)
    return WeiboExtractor(cookie_str=cookie_str or None)


def _user_to_dict(user) -> dict:
    return {
        "uid": str(user.id),
        "screen_name": user.screen_name,
        "avatar_url": user.avatar_url or "",
        "verified": bool(user.verified),
        "verified_reason": user.verified_reason or "",
        "followers_count": user.followers_count,
        "posts_count": user.posts_count,
        "description": (user.description or "")[:120],
    }


def _sort_verified_first(users: list) -> list:
    """带 V 优先；同组保持接口原始顺序（粉丝数是展示文案无法排序）。"""
    return sorted(users, key=lambda u: not bool(u.verified))


@router.post("/users")
async def search_users(req: SearchUsersRequest, request: Request) -> dict:
    check_profile(request)
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")
    extractor = _build_extractor()

    def _run() -> list[dict]:
        users = extractor.client.search_users(query, page=1)
        return [_user_to_dict(u) for u in _sort_verified_first(users)]

    try:
        results = await asyncio.to_thread(_run)
    except Exception as exc:
        kind = classify_error(exc)
        logger.warning("博主搜索失败: %s", exc)
        if kind == WeiboErrorKind.RATE_LIMIT:
            raise HTTPException(status_code=429, detail="搜索请求过于频繁，请稍后再试") from exc
        raise HTTPException(status_code=400, detail=f"搜索失败：{exc}") from exc
    return {"query": query, "results": results}


@router.post("/resolve")
async def resolve_target(req: ResolveTargetRequest, request: Request) -> dict:
    """链接/分享文本/纯 UID → 目标博主公开资料。"""
    check_profile(request)
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容不能为空")
    extractor = _build_extractor()

    def _run() -> dict:
        if _UID_RE.fullmatch(text):
            uid = text
        elif _URL_HINT_RE.search(text):
            uid = extractor.resolve_url(text)
        else:
            raise WeiboError(
                "无法识别为微博主页链接或 UID，请改用搜索",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        info = extractor.get_user_info(uid)
        return {
            "uid": info.uid,
            "screen_name": info.screen_name,
            "avatar_url": info.avatar_url or "",
            "verified": bool(info.verified),
            "verified_reason": info.verified_reason or "",
            "followers_count": info.followers_count,
            "posts_count": info.posts_count,
            "description": (info.description or "")[:120],
        }

    try:
        return await asyncio.to_thread(_run)
    except WeiboError as exc:
        status = (
            401 if exc.kind == WeiboErrorKind.AUTH
            else 404 if exc.kind == WeiboErrorKind.NOT_FOUND
            else 400
        )
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("目标识别失败: %s", exc)
        raise HTTPException(status_code=400, detail=f"识别失败：{exc}") from exc
