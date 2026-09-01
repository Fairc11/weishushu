# -*- coding: utf-8 -*-
"""微博书 - 错误分类与重试工具。"""

from __future__ import annotations

import logging
import re
import time
from enum import Enum
from typing import Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)
T = TypeVar("T")


class WeiboErrorKind(Enum):
    """错误分类枚举。"""

    NETWORK = "network"
    API = "api"
    AUTH = "auth"
    PARSE = "parse"
    RATE_LIMIT = "rate_limit"
    NOT_FOUND = "not_found"
    BROWSER = "browser"
    UNKNOWN = "unknown"


class WeiboError(Exception):
    """带分类的微博书异常。"""

    def __init__(
        self,
        message: str,
        kind: WeiboErrorKind = WeiboErrorKind.UNKNOWN,
        original: Optional[Exception] = None,
        recoverable: bool = True,
    ):
        super().__init__(message)
        self.kind = kind
        self.original = original
        self.recoverable = recoverable

    def __str__(self) -> str:
        prefix_map = {
            WeiboErrorKind.NETWORK: "[网络]",
            WeiboErrorKind.API: "[API]",
            WeiboErrorKind.AUTH: "[认证]",
            WeiboErrorKind.PARSE: "[解析]",
            WeiboErrorKind.RATE_LIMIT: "[限流]",
            WeiboErrorKind.NOT_FOUND: "[未找到]",
            WeiboErrorKind.BROWSER: "[浏览器]",
            WeiboErrorKind.UNKNOWN: "[未知]",
        }
        return f"{prefix_map.get(self.kind, '')}{self.args[0]}"


class OperationCancelled(Exception):
    """后台任务的协作式停止信号，不写失败报告也不转换为业务错误。"""


class OperationPaused(Exception):
    """后台任务到达安全点后的暂停信号，不转换为业务失败。"""

    def __init__(self, message: str, *, pause_reason: str = "user_requested") -> None:
        super().__init__(message)
        self.pause_reason = pause_reason


def classify_error(exc: Exception) -> WeiboErrorKind:
    """根据异常类型和消息推断错误分类。"""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()

    if msg == "encountered 432 anti-crawler block":
        return WeiboErrorKind.RATE_LIMIT

    try:
        from crawl4weibo.exceptions.base import ParseError
    except ImportError:  # pragma: no cover - 运行依赖必定存在
        ParseError = ()  # type: ignore[assignment]
    if isinstance(exc, ParseError):
        if re.fullmatch(r"Post \S+ not found", str(exc)):
            return WeiboErrorKind.NOT_FOUND
        return WeiboErrorKind.PARSE

    if any(
        key in msg
        for key in (
            "timeout",
            "timed out",
            "connection refused",
            "network",
            "dns",
            "unable to resolve",
            "connection reset",
            "broken pipe",
            "remote end closed",
            # 中文兜底（v1.1.1：中文用户最常看到的错误）
            "超时",
            "连接被拒",
            "网络",
            "无法解析",
            "重置",
            "断网",
            "网络中断",
        )
    ):
        return WeiboErrorKind.NETWORK

    if any(name_key in name for name_key in ("httpx", "requests", "aiohttp")):
        if "401" in msg or "403" in msg or "unauthorized" in msg:
            return WeiboErrorKind.AUTH
        if "404" in msg:
            return WeiboErrorKind.NOT_FOUND
        if "429" in msg or "too many" in msg:
            return WeiboErrorKind.RATE_LIMIT
        return WeiboErrorKind.NETWORK

    if any(key in msg for key in ("cookie", "login", "auth", "登录", "未登录", "权限", "403")):
        return WeiboErrorKind.AUTH
    if any(key in msg for key in ("api", "error_code", "errno", "sina.com.cn")):
        return WeiboErrorKind.API
    if any(key in msg for key in ("rate", "limit", "too many", "429", "频率", "频繁")):
        return WeiboErrorKind.RATE_LIMIT
    if any(key in msg for key in ("not found", "404", "不存在", "未找到")):
        return WeiboErrorKind.NOT_FOUND
    if any(key in msg for key in ("parse", "json", "decode", "schema", "解析", "格式", "invalid")):
        return WeiboErrorKind.PARSE
    return WeiboErrorKind.UNKNOWN


def is_recoverable(kind: WeiboErrorKind) -> bool:
    """判断错误是否值得重试。"""
    return kind in (
        WeiboErrorKind.NETWORK,
        WeiboErrorKind.API,
        WeiboErrorKind.RATE_LIMIT,
        WeiboErrorKind.UNKNOWN,
    )


def get_retry_delay(attempt: int, base: float = 1.0, max_delay: float = 60.0) -> float:
    """指数退避延迟，带轻微随机抖动。"""
    import random

    return min(base * (2**attempt) + random.uniform(0, 1), max_delay)


def retry_with_backoff(
    func: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: Optional[list[WeiboErrorKind]] = None,
) -> T:
    """执行函数并在可恢复错误上指数退避重试。"""
    last_exc: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            kind = classify_error(exc)
            if retry_on is not None and kind not in retry_on:
                raise
            if not is_recoverable(kind) and attempt > 0:
                raise
            if attempt < max_attempts - 1:
                delay = get_retry_delay(attempt, base_delay, max_delay)
                logger.warning(
                    "尝试 %s/%s 失败 (%s): %s，%.1f 秒后重试...",
                    attempt + 1,
                    max_attempts,
                    kind.value,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error("所有 %s 次尝试均失败: %s", max_attempts, exc)

    # 重抛最后一个异常，保持原始 kind 分类（不吞成 RuntimeError 违铁律）
    if last_exc is not None:
        raise last_exc
    # 极端边界：max_attempts=0 或循环未进入
    raise WeiboError(WeiboErrorKind.UNKNOWN, "重试耗尽但无异常记录")


def network_check(timeout: float = 5.0) -> bool:
    """检测 m.weibo.cn 是否基本可达。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get("https://m.weibo.cn", follow_redirects=True)
            return resp.status_code < 500
    except Exception:
        return False
