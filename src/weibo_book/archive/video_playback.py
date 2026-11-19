"""PC ``ajax/statuses/show`` 的 ``playback_list`` 高清视频源解析。

2026-09-08 实测（样本 bid=RcDyYpZqs）：m.weibo.cn 的 statuses/show 只返回
480p（label=mp4_hd）；PC 端 ``https://weibo.com/ajax/statuses/show?id=<bid>``
返回 ``page_info.media_info.playback_list``，含 1080p/720p/480p 直链：

- 1080p ``label=mp4_1080p&template=1920x1080``
- 720p  ``label=mp4_720p&template=1280x720``
- 480p  ``label=mp4_hd&template=852x480``
"""

from __future__ import annotations

import re

_TEMPLATE_RE = re.compile(r"[?&]template=(\d+)x(\d+)")
_LABEL_RE = re.compile(r"[?&]label=([\w]+)")
_LABEL_RANK = {
    "mp4_ld": 1,
    "mp4_sd": 1,
    "mp4_hd": 2,
    "mp4_720p": 3,
    "mp4_1080p": 4,
}


def video_quality_score(url: object) -> tuple[int, int]:
    """视频 URL 画质分：``(分辨率面积, 清晰度档位)``，越大越清晰。"""
    if not isinstance(url, str):
        return (0, 0)
    area = 0
    match = _TEMPLATE_RE.search(url)
    if match:
        area = int(match.group(1)) * int(match.group(2))
    label = _LABEL_RE.search(url)
    rank = _LABEL_RANK.get(label.group(1), 0) if label else 0
    return (area, rank)


def parse_best_playback_url(payload: object) -> str | None:
    """从 ``ajax/statuses/show`` 响应挑最高画质的 mp4 直链；没有则 ``None``。"""
    if not isinstance(payload, dict):
        return None
    page_info = payload.get("page_info")
    if not isinstance(page_info, dict):
        return None
    media_info = page_info.get("media_info")
    if not isinstance(media_info, dict):
        return None
    playback_list = media_info.get("playback_list")
    if not isinstance(playback_list, list):
        return None
    best_url: str | None = None
    best_score = (0, 0)
    for item in playback_list:
        if not isinstance(item, dict):
            continue
        play_info = item.get("play_info")
        if not isinstance(play_info, dict):
            continue
        mime = play_info.get("mime")
        url = play_info.get("url")
        if not isinstance(mime, str) or not mime.startswith("video/"):
            continue
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        score = video_quality_score(url)
        if score > best_score:
            best_score = score
            best_url = url
    return best_url
