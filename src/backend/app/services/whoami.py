"""v1.1.5 whoami 服务。

从当前 cookie 拿"本人 UID"——**不**接用户输入 URL。
复用现有 weibo_book.api.WeiboBook() + ensure_login(force=False)（v1.1.2 静默登录）。
无 cookie / 过期 → 抛 WeiboError(kind=AUTH)。
"""
from __future__ import annotations

import logging

from weibo_book import WeiboBook
from weibo_book.errors import WeiboError, WeiboErrorKind, classify_error
from weibo_book.extractor import WeiboExtractor

logger = logging.getLogger(__name__)


def whoami() -> dict:
    """返回当前登录用户信息（uid/screen_name/avatar_url/posts_count/followers_count）。

    复用链：
    - load_cookies() (weibo_book/login.py, v1.1.1 S2 icacls 收紧)
    - WeiboBook.ensure_login(force=False) (v1.1.2 F1 静默登录)
    - WeiboExtractor.get_user_info() 拿 UID
    """
    try:
        book = WeiboBook()
        cookie_str = book.ensure_login(force=False)  # 静默：有就用，无就 None
    except Exception as e:
        logger.warning("whoami: ensure_login 失败: %s", e)
        raise WeiboError(
            f"未登录或登录态已过期: {e}",
            kind=WeiboErrorKind.AUTH,
            original=e,
        )

    if not cookie_str:
        raise WeiboError(
            "未登录：未找到 cookie 文件，请先扫码或从 Chrome 导入",
            kind=WeiboErrorKind.AUTH,
        )

    # 用 cookie 拿 self_uid
    try:
        ext = WeiboExtractor(cookie_str=cookie_str)
        # 走 weibo.cn/api/config 拿当前登录 UID（v1.1.1 login.check_cookies_valid 同思路）
        r = ext.client.session.get(
            "https://m.weibo.cn/api/config",
            timeout=15,
        )
        r.raise_for_status()
        cfg = r.json()
        data = cfg.get("data", {}) or {}
        if not data.get("login", False):
            raise WeiboError("cookie 已过期，登录态失效", kind=WeiboErrorKind.AUTH)
        # data.uid 字段
        self_uid = str(data.get("uid", "")).strip()
        if not self_uid:
            raise WeiboError("无法从 /api/config 拿 self_uid", kind=WeiboErrorKind.NOT_FOUND)
        # 拿用户详情
        user = ext.get_user_info(self_uid)
        return {
            "uid": user.uid,
            "screen_name": user.screen_name,
            "avatar_url": user.avatar_url,
            "followers_count": user.followers_count,
            "following_count": user.following_count,
            "posts_count": user.posts_count,
            "verified": user.verified,
            "description": user.description,
        }
    except WeiboError:
        raise
    except Exception as e:
        kind = classify_error(e)
        if kind == WeiboErrorKind.AUTH:
            raise WeiboError(f"登录态失效: {e}", kind=WeiboErrorKind.AUTH, original=e)
        raise WeiboError(f"whoami 失败: {e}", kind=kind, original=e)
