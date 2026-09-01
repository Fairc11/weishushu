"""核心：preview（同步）+ start（异步任务）。

进度回调关键链路：
  WeiboBook.generate(progress_callback=on_progress)
    → task_manager.update_progress(task_id, pct, msg)
    → ws_manager.broadcast(task_id, {...})
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from backend.app.config import settings
from backend.app.features import raise_future_feature
from backend.app.schemas import PreviewRequest, StartRequest, StartResponse
from backend.app.services.rate_limit import check_scraper
from backend.app.services.task_manager import run_in_background, task_manager
from weibo_book.models import ExtractType, ImageQuality

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scrape", tags=["scraper"])


def _preview_to_dict(p: dict) -> dict:
    """extractor.preview_posts 返回 [{bid, text, created_at, ...}] 形态的 dict。"""
    return {
        "bid": p.get("bid", ""),
        "text": p.get("text", ""),
        "created_at": str(p.get("created_at", "")),
        "reposts_count": p.get("reposts_count", 0),
        "comments_count": p.get("comments_count", 0),
        "likes_count": p.get("likes_count", 0),
        "is_original": p.get("is_original", True),
        "media_count": p.get("media_count", len(p.get("media", [])) if isinstance(p.get("media"), list) else 0),
    }


def _coerce_extract_type(s: str) -> ExtractType:
    """前端 / API 传字符串 → 业务核心要 ExtractType 枚举。

    v1.2.0 P0-2 修复：M3 整改前 router 传 "posts" 字符串，业务核心访问
    `extract_type.value` 会触发 `AttributeError: 'str' object has no attribute 'value'`。
    """
    if s == "posts":
        return ExtractType.POSTS
    if s == "favorites":
        return ExtractType.FAVORITES
    raise HTTPException(
        status_code=400,
        detail=f"extract_type 必须是 posts 或 favorites: {s!r}",
    )


def _coerce_image_quality(s: str) -> ImageQuality:
    """前端图片质量字符串 → 业务核心 ImageQuality 枚举。"""
    try:
        return ImageQuality(s)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"image_quality 必须是 thumb180/mw690/mw1024/large/original: {s!r}",
        )


@router.post("/preview")
async def preview(req: PreviewRequest, request: Request) -> dict:
    """URL → 用户 + 20 条预览。同步调用，1-3s 内返回。"""
    raise_future_feature()
    check_scraper(request)  # S4 v1.1.1：限流 3 次/分钟/IP
    from weibo_book import WeiboBook
    from weibo_book.errors import WeiboError, WeiboErrorKind

    try:
        book = WeiboBook()
        data = await asyncio.to_thread(book.preview_posts, req.url, count=req.count)
    except WeiboError as we:
        # L4 v1.1.1：WeiboError 分类映射 → 4xx
        status = 401 if we.kind == WeiboErrorKind.AUTH else 404 if we.kind == WeiboErrorKind.NOT_FOUND else 400
        raise HTTPException(status_code=status, detail=str(we))
    except Exception as e:
        logger.exception("preview 失败: %s", e)
        raise HTTPException(status_code=400, detail=f"预览失败: {e}")

    user = data.get("user")
    previews = data.get("previews", [])
    return {
        "user": {
            "uid": user.uid,
            "screen_name": user.screen_name,
            "avatar_url": user.avatar_url,
            "posts_count": user.posts_count,
            "followers_count": user.followers_count,
        },
        "previews": [_preview_to_dict(p) for p in previews],
        # L1 + L3 v1.1.1：partial 标记
        "_partial": data.get("_partial", False),
        "_partial_reason": data.get("_partial_reason", ""),
    }


@router.post("/start", response_model=StartResponse)
async def start(req: StartRequest, request: Request, bg: BackgroundTasks) -> StartResponse:
    """URL + 配置 → 创建任务 → 后台跑。客户端用 task_id 订阅 WS 拿进度。"""
    raise_future_feature()
    check_scraper(request)  # S4 v1.1.1：限流 3 次/分钟/IP
    task_id = await task_manager.create()
    output_dir = str(settings.output_dir)
    loop = asyncio.get_running_loop()

    def _progress_cb(pct: float, msg: str) -> None:
        # progress_callback 在业务线程跑，要切到 event loop 调 async 接口
        try:
            future = asyncio.run_coroutine_threadsafe(
                task_manager.update_progress(task_id, pct, msg), loop
            )
            future.add_done_callback(
                lambda f: logger.debug("progress 回调失败: %s", f.exception())
                if f.exception() else None
            )
        except Exception as e:
            logger.warning("progress 回调失败: %s", e)

    def _progress_event_cb(event: dict) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                task_manager.update_progress_event(task_id, event), loop
            )
            future.add_done_callback(
                lambda f: logger.debug("结构化进度回调失败: %s", f.exception())
                if f.exception() else None
            )
        except Exception as e:
            logger.warning("结构化进度回调失败: %s", e)

    async def _run() -> dict:
        from weibo_book import WeiboBook

        book = WeiboBook(image_quality=_coerce_image_quality(req.image_quality))
        book._progress_event_callback = _progress_event_cb
        rec = task_manager.get(task_id)
        if rec is not None:
            book._cancel_event = rec._cancel_event
        return await asyncio.to_thread(
            book.generate,
            url=req.url,
            max_posts=req.max_posts,
            output_dir=output_dir,
            formats=req.formats,
            comments=req.comments,
            comments_count=req.comments_count,
            comments_type=req.comments_type,
            download_media=req.download_media,
            login=req.login,
            start_date=req.start_date,
            end_date=req.end_date,
            only_original=req.only_original,
            extract_type=_coerce_extract_type(req.extract_type),
            post_ids=req.post_ids,
            progress_callback=_progress_cb,
        )

    bg.add_task(run_in_background, task_id, _run)
    return StartResponse(task_id=task_id)
