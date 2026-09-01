"""Navigation policy for the embedded Weibo browser."""

from __future__ import annotations

from urllib.parse import urlparse


ALLOWED_HOST_ROOTS = (
    "weibo.com",
    "weibo.cn",
    "sina.com.cn",
    "sina.cn",
)


def is_allowed_weibo_host(host: str) -> bool:
    normalized = (host or "").strip().rstrip(".").lower()
    return any(
        normalized == root or normalized.endswith(f".{root}")
        for root in ALLOWED_HOST_ROOTS
    )


def is_allowed_browser_url(url: str) -> bool:
    value = (url or "").strip()
    if value == "about:blank":
        return True
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return is_allowed_weibo_host(parsed.hostname or "")
