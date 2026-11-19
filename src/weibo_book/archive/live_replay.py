"""微博直播回放的解析工具。

直播微博的 ``page_info.type == 'live'``，m.weibo.cn 只给落地页链接
``https://weibo.com/l/wblive/p/show/1022:<live_id>``；回放地址要走 PC 接口
``/l/!/2/wblive/room/show_pc_live.json``，返回 HLS（m3u8 + ts 分段），
没有 mp4 直链。本模块只做纯解析，网络与落盘在 source/sync 里。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

# 直播落地页里的直播标识，例如 ``1022:2321325340611543957577``
_LIVE_PAGE_RE = re.compile(r"weibo\.com/l/wblive/p/show/(1022:\d+)")

# 单次补抓的分段上限，防止异常回放塞爆档案
MAX_REPLAY_SEGMENTS = 2000


def parse_live_id(url: object) -> str | None:
    """从直播落地页链接提取 ``1022:<live_id>``；非微博直播页返回 ``None``。"""
    if not isinstance(url, str):
        return None
    match = _LIVE_PAGE_RE.search(url)
    return match.group(1) if match is not None else None


def parse_replay_payload(payload: object) -> dict[str, str] | None:
    """解析 ``show_pc_live.json`` 响应，返回回放信息；无回放返回 ``None``。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    replay_url = data.get("replay_origin_url")
    if not isinstance(replay_url, str) or not replay_url.startswith("http"):
        return None
    result = {"replay_url": replay_url}
    for key in ("cover", "title"):
        value = data.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def parse_m3u8_segments(text: str) -> list[str]:
    """返回 m3u8 里的分段相对地址（非注释非空行），按原顺序。"""
    segments = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(line)
    return segments


def rewrite_m3u8(text: str, segment_names: list[str]) -> str:
    """把 m3u8 里的分段行改写为本地文件名；其余行原样保留。"""
    names = iter(segment_names)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(next(names))
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def resolve_segment_url(playlist_url: str, segment: str) -> str:
    """把分段相对地址解析成绝对 URL。"""
    return urljoin(playlist_url, segment)
