"""v1.2.0 V120-3: cookie 注入机制 API。

端点：
- POST /api/browser/inject   读 cookies.json，提取关键 cookie 返给前端

前端拿到 cookie 后调 window.pywebview.api.inject_cookies(cookies) 注入浏览器。
关键 cookie 名单（微博登录必须）：SUB / SUBP / ALF / _T_WM / SSOLoginState

跨设备拦截说明（v1.2.0 风险红线 §1.5）：
- cookie 文件只读取本机路径候选，不支持外部同步文件
- 跨设备同步 → 拒做（详见 RISKS.md §1.5 + §1.8-10）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.app.features import raise_future_feature
from backend.app.platform_paths import cookie_file_candidates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser", tags=["browser"])

# 微博登录关键 cookie（缺一不可登录态）
KEY_COOKIE_NAMES = ("SUB", "SUBP", "ALF", "_T_WM", "SSOLoginState")


def _find_cookies_file() -> Path:
    """按统一候选列表找 cookie 文件；没有命中时返回主 cookie 落点。"""
    candidates = cookie_file_candidates()
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


@router.post("/inject")
async def api_browser_inject() -> dict:
    """V120-3: 读 cookies.json，提取关键 cookie 返给前端。

    前端拿到 cookie 后调 window.pywebview.api.inject_cookies(cookies) 注入浏览器。
    关键 cookie 5 个：SUB / SUBP / ALF / _T_WM / SSOLoginState，缺一 → 404。
    """
    raise_future_feature()
    cookie_path = _find_cookies_file()
    if not cookie_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"cookies.json 不存在: {cookie_path}（请先登录）",
        )

    try:
        data = json.loads(cookie_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"cookies.json 格式错: {e}",
        )

    # 兼容 list[dict] 和 dict[str, str] 两种格式
    if isinstance(data, dict):
        cookies = [
            {"name": k, "value": v, "domain": ".weibo.cn", "path": "/"}
            for k, v in data.items()
        ]
    elif isinstance(data, list):
        cookies = data
    else:
        raise HTTPException(
            status_code=400,
            detail=f"cookies.json 格式不支持: {type(data).__name__}",
        )

    # 提取关键 cookie
    key_cookies = [c for c in cookies if c.get("name") in KEY_COOKIE_NAMES]

    if not key_cookies:
        raise HTTPException(
            status_code=404,
            detail=f"关键 cookie 缺失（需要 {list(KEY_COOKIE_NAMES)}，找到 0 个）",
        )

    return {
        "ok": True,
        "cookies": key_cookies,
        "total_found": len(cookies),
        "key_found": len(key_cookies),
        "source_path": str(cookie_path),
    }
