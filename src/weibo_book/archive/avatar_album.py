"""微博「头像相册」（photo.weibo.com 只读接口）的响应解析。

2026-09-07 实测确认的形状：

- ``GET /albums/get_all?uid=<uid>&page=1&count=30`` 返回
  ``{"result": true, "data": {"total": n, "album_list": [...]}}``，
  其中 ``caption == "头像相册"``（type=18）的条目给出 ``album_id``。
- ``GET /photos/get_all?uid=<uid>&album_id=<id>&page=1&count=100`` 返回
  ``{"result": true, "data": {"total": n, "photo_list": [...]}}``，
  每张照片含 ``pic_host`` + ``pic_name``（大图 URL 为
  ``<pic_host>/large/<pic_name>``）与 ``timestamp``（上传时间，秒级）。
"""

from __future__ import annotations

from datetime import datetime, timezone

AVATAR_ALBUM_CAPTION = "头像相册"
ALBUM_LIST_URL = "https://photo.weibo.com/albums/get_all"
PHOTO_LIST_URL = "https://photo.weibo.com/photos/get_all"
PHOTO_PAGE_SIZE = 100
MAX_PHOTO_PAGES = 2


def parse_avatar_album_id(payload: object) -> str | None:
    """从相册列表响应中找「头像相册」的 album_id；没有则返回 ``None``。"""
    if not isinstance(payload, dict) or payload.get("result") is not True:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    album_list = data.get("album_list")
    if not isinstance(album_list, list):
        return None
    for album in album_list:
        if not isinstance(album, dict):
            continue
        album_id = album.get("album_id")
        if album.get("caption") == AVATAR_ALBUM_CAPTION and isinstance(album_id, str) and album_id:
            return album_id
    return None


def parse_photo_page(payload: object) -> tuple[list[tuple[str, str | None]], int]:
    """解析一页专辑照片，返回 ``([(大图 URL, 上传时间 ISO 或 None)], total)``。"""
    if not isinstance(payload, dict) or payload.get("result") is not True:
        return [], 0
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], 0
    total = data.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = 0
    photo_list = data.get("photo_list")
    if not isinstance(photo_list, list):
        return [], total
    photos: list[tuple[str, str | None]] = []
    for photo in photo_list:
        if not isinstance(photo, dict):
            continue
        url = _large_pic_url(photo)
        if url is None:
            continue
        photos.append((url, _photo_iso_time(photo)))
    return photos, total


def _large_pic_url(photo: dict) -> str | None:
    host = photo.get("pic_host")
    name = photo.get("pic_name")
    if (
        not isinstance(host, str)
        or not host.startswith("https://")
        or not isinstance(name, str)
        or not name
        or "/" in name
        or ".." in name
    ):
        return None
    return f"{host}/large/{name}"


def _photo_iso_time(photo: dict) -> str | None:
    timestamp = photo.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
