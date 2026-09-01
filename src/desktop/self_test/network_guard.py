"""自检网络守卫：只允许回环地址。"""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


def assert_loopback(url: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if not is_loopback_host(host):
        raise ValueError(f"自检禁止非回环地址: {url}")
