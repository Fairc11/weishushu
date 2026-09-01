"""CDN 防盗链代理。微博图片 / 视频 / 头像直接 fetch 会被 403，加 Referer 即可。"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assets", tags=["assets"])

# 白名单：sinaimg.cn 系列 + weibo 自身
ALLOWED_DOMAINS = {
    "sinaimg.cn",
    "wx1.sinaimg.cn", "wx2.sinaimg.cn", "wx3.sinaimg.cn", "wx4.sinaimg.cn",
    "ww1.sinaimg.cn", "ww2.sinaimg.cn", "ww3.sinaimg.cn", "ww4.sinaimg.cn",
    "wxa1.sinaimg.cn", "wxa2.sinaimg.cn",
    "tvax1.sinaimg.cn", "tvax2.sinaimg.cn", "tvax3.sinaimg.cn", "tvax4.sinaimg.cn",
    "weibo.com", "m.weibo.cn", "sina.com.cn",
    "video.weibo.com", "f.us.sinaimg.cn",
    "h5.sinaimg.cn",
}


def _is_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    # 顶级域匹配 + 子域通配（*.sinaimg.cn）
    if host in ALLOWED_DOMAINS:
        return True
    for allowed in ALLOWED_DOMAINS:
        if host.endswith("." + allowed):
            return True
    return False


REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5  # 防止重定向环（防御性，正常一次就够）


@router.get("/img")
async def proxy_image(url: str = Query(..., min_length=1)) -> Response:
    """图床代理：url → 字节流。带防盗链 Referer。

    v1.2.0 P0 (B-02)：原版 `follow_redirects=True` + 白名单只检首跳，攻击者
    可用 `sinaimg.cn/...302→evil.com` 绕过白名单拉内网。修法：
    - `follow_redirects=False`，手动循环重定向
    - 每一跳的 Location host 都必须过白名单
    - 超过 MAX_REDIRECTS 直接 502
    """
    if not _is_allowed(url):
        raise HTTPException(status_code=400, detail=f"domain not allowed: {url}")

    current_url = url
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 Weishushu/1.1.1",
                "Referer": "https://weibo.com/",
            },
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                r = await client.get(current_url)
                if r.status_code in REDIRECT_STATUSES:
                    # 手动校验每个 Location
                    location = r.headers.get("location") or r.headers.get("Location")
                    if not location:
                        # 3xx 但无 Location → 视为错误响应
                        r.raise_for_status()
                    if not _is_allowed(location):
                        raise HTTPException(
                            status_code=400,
                            detail=f"redirect target not allowed: {location}",
                        )
                    # 拼接相对 URL（Location 可能是 /path 形式）
                    if not location.startswith(("http://", "https://")):
                        # 用 urlparse 拼绝对 URL
                        from urllib.parse import urljoin
                        current_url = urljoin(current_url, location)
                    else:
                        current_url = location
                    continue
                # 终态：非重定向
                r.raise_for_status()
                return Response(
                    content=r.content,
                    media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=3600"},
                )
            # 重定向环
            raise HTTPException(status_code=502, detail="too many redirects")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("proxy_image 失败: %s", e)
        raise HTTPException(status_code=502, detail=f"proxy failed: {e}")
