"""从 SQLite 当前状态生成完全离线、只读的微博书快照。"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from html import escape
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import quote, unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from weibo_book.errors import OperationCancelled, OperationPaused

from .media_layout import media_path_shape
from .paths import ArchivePaths
from .profile_assets import profile_asset_identity
from .video_playback import video_quality_score
from .presentation import format_archive_time, normalize_archive_text
from .repository import ArchiveError, ArchiveRepository

logger = logging.getLogger(__name__)
_PDF_PRINT_BATCH_SIZE = 50
HLS_LIBRARY_NAME = "hls.min.js"
# 互动 HTML 版式号：V1 = v1.x 旧索引档案；V2 = v2.0.0/v2.0.1；V3 = v2.1.0（含搜索与超话页签）；
# V4 = v2.2.0（合并导航与桌面三栏）。只升不降；每次互动模板能力变化在发布时递增。
BOOK_FORMAT_VERSION = 4
BOOK_FORMAT_META_KEY = "book_format_version"
_ARCHIVE_MONTH = re.compile(
    r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])-\d{2} \d{2}:\d{2}$"
)
_SUPPORTS_DIRECTORY_FDS = (
    os.name != "nt"
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)


class UnsafeMediaPathError(ArchiveError):
    """PDF 缩略图源路径越界或使用链接。"""


def _check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise OperationCancelled("任务已取消")


def _combined_control(
    cancel_requested: Callable[[], bool] | None,
    pause_requested: Callable[[], bool] | None,
) -> Callable[[], bool]:
    def requested() -> bool:
        if pause_requested is not None and pause_requested():
            raise OperationPaused("任务已暂停")
        return cancel_requested is not None and cancel_requested()

    return requested


@contextmanager
def _open_archive_media(media_base: Path, local_path: str):
    """在归档媒体基准目录内打开非链接普通媒体文件。"""
    physical_root = media_base.resolve(strict=True)
    parts = PurePosixPath(_safe_local_path(local_path)).parts
    directory_descriptors: list[int] = []
    descriptor: int | None = None
    checked_markers: list[os.stat_result] = []
    try:
        if os.name == "nt":
            cursor = physical_root.joinpath(*parts)
            for index in range(1, len(parts) + 1):
                marker = physical_root.joinpath(*parts[:index]).lstat()
                if stat.S_ISLNK(marker.st_mode):
                    raise UnsafeMediaPathError(
                        "PDF 缩略图源文件不能是符号链接"
                    )
                expected = (
                    stat.S_ISREG(marker.st_mode)
                    if index == len(parts)
                    else stat.S_ISDIR(marker.st_mode)
                )
                if not expected:
                    raise UnsafeMediaPathError("PDF 缩略图源路径类型错误")
                checked_markers.append(marker)
            descriptor = os.open(
                cursor, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        else:
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            root_descriptor = os.open(physical_root, directory_flags)
            directory_descriptors.append(root_descriptor)
            for part in parts[:-1]:
                child_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_descriptors[-1],
                )
                marker = os.fstat(child_descriptor)
                if not stat.S_ISDIR(marker.st_mode):
                    os.close(child_descriptor)
                    raise UnsafeMediaPathError("PDF 缩略图源路径类型错误")
                directory_descriptors.append(child_descriptor)
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptors[-1],
            )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise UnsafeMediaPathError("PDF 缩略图源文件在打开时已变化")
        if os.name == "nt":
            expected_file = checked_markers[-1]
            if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                expected_file.st_dev,
                expected_file.st_ino,
                expected_file.st_mode,
            ):
                raise UnsafeMediaPathError("PDF 缩略图源文件在打开时已变化")
            for index, expected_marker in enumerate(checked_markers, start=1):
                current = physical_root.joinpath(*parts[:index]).lstat()
                if (
                    stat.S_ISLNK(current.st_mode)
                    or (current.st_dev, current.st_ino, current.st_mode)
                    != (
                        expected_marker.st_dev,
                        expected_marker.st_ino,
                        expected_marker.st_mode,
                    )
                ):
                    raise UnsafeMediaPathError("PDF 缩略图源路径在打开时已变化")
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeMediaPathError(
                "PDF 缩略图源文件不能是符号链接或无效路径", original=exc
            ) from exc
        raise ArchiveError("PDF 缩略图源文件不可用", original=exc) from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
    assert descriptor is not None
    stream = os.fdopen(descriptor, "rb")
    try:
        yield stream
    finally:
        stream.close()


class FrozenDict(dict):
    """保持 JSON 对象语义，同时拒绝快照生成后的原位修改。"""

    def _readonly(self, *args, **kwargs):
        raise TypeError("归档渲染快照只读")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly
    __ior__ = _readonly


def _freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _archive_month(value: object) -> tuple[str, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _ARCHIVE_MONTH.fullmatch(value)
    if match is None:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    return f"{year:04d}-{month:02d}", year, month


def _build_timeline_index(posts: list[dict]) -> dict:
    normal_posts = [post for post in posts if post["is_pinned"] is not True]
    months: list[dict] = []
    for index, post in enumerate(normal_posts):
        parsed = _archive_month(post.get("created_at"))
        if parsed is None:
            continue
        key, year, month = parsed
        if months and months[-1]["key"] == key:
            months[-1]["end"] = index + 1
            continue
        months.append(
            {
                "key": key,
                "year": year,
                "month": month,
                "start": index,
                "end": index + 1,
            }
        )
    return {"months": months, "normal_count": len(normal_posts)}


def _safe_local_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or ":" in value
        or "?" in value
        or "#" in value
        or Path(value).is_absolute()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ArchiveError("归档媒体路径不安全")
    path = PurePosixPath(value)
    if media_path_shape(value) is None:
        raise ArchiveError("归档媒体路径不安全")
    return path.as_posix()


def _browser_url(value: str, prefix: str = "") -> str:
    path = _safe_local_path(value)
    if prefix:
        path = prefix + path
    return "/".join(quote(segment, safe="-._~") for segment in path.split("/"))


def _as_file_uri(path) -> str:
    """按 pathlib 的平台规则生成 file URI，不手工拼接盘符或转义。"""
    return path.as_uri()


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(payload: dict, key: str) -> str:
    value = payload.get(key, "")
    return value if isinstance(value, str) else ""


def _count(payload: dict, key: str) -> int:
    value = payload.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _local(payload: dict, key: str) -> str:
    value = payload.get(key)
    return _safe_local_path(value) if isinstance(value, str) and value else ""


def _project_link(payload: object, prefix: str = "") -> dict | None:
    if not isinstance(payload, dict):
        return None
    if _text(payload, "type") in ("topic", "search_topic"):
        # 话题页卡片不渲染（微博正文中的 #话题# 本身已足够表达）
        return None
    external = ""
    for key in ("url", "original_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            external = value
            break
    result = {
        "type": _text(payload, "type"),
        "title": _text(payload, "title"),
        "description": _text(payload, "description"),
        "url": external,
        "local_image": _local(payload, "local_image"),
        "browser_url": _browser_url(_local(payload, "local_image"), prefix)
        if _local(payload, "local_image") else "",
    }
    return result


def _project_payload_media(payload: object, prefix: str = "") -> list[dict]:
    if not isinstance(payload, list):
        return []
    result = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        position = item.get("position", 0)
        position = position if isinstance(position, int) else 0
        kind = _text(item, "type")
        local_path = _local(item, "local_path")
        local_thumb = _local(item, "local_thumb")
        video_cover = _local(item, "video_cover")
        if kind == "image" and local_path:
            result.append({"kind": "image", "position": position, "local_path": local_path, "browser_url": _browser_url(local_path, prefix)})
        elif kind == "video" and local_path:
            result.append({
                "kind": "video", "position": position,
                "local_path": local_path, "browser_url": _browser_url(local_path, prefix),
                "cover_path": video_cover or local_thumb,
                "cover_url": _browser_url(video_cover or local_thumb, prefix) if video_cover or local_thumb else "",
            })
        elif kind == "live_photo" and local_path and local_thumb:
            result.append({
                "kind": "live_photo", "position": position,
                "video_path": local_path, "video_url": _browser_url(local_path, prefix),
                "image_path": local_thumb, "image_url": _browser_url(local_thumb, prefix),
            })
        elif kind == "live_photo" and local_path:
            result.append({
                "kind": "video", "position": position,
                "local_path": local_path, "browser_url": _browser_url(local_path, prefix),
                "cover_path": video_cover, "cover_url": _browser_url(video_cover, prefix) if video_cover else "",
            })
        elif kind == "live_photo" and local_thumb:
            result.append({"kind": "image", "position": position, "local_path": local_thumb, "browser_url": _browser_url(local_thumb, prefix)})
        elif kind:
            result.append({"kind": "unavailable", "position": position})
    return result


def _project_retweeted(payload: object, prefix: str = "") -> dict | None:
    if not isinstance(payload, dict):
        return None
    return {
        "bid": _text(payload, "bid"),
        "uid": _text(payload, "uid"),
        "user_name": normalize_archive_text(_text(payload, "user_name")),
        "text": normalize_archive_text(_text(payload, "text")),
        "created_at": format_archive_time(_text(payload, "created_at")),
        "source": _text(payload, "source"),
        "ip_location": _text(payload, "ip_location"),
        "visibility": _text(payload, "visibility"),
        "reposts_count": _count(payload, "reposts_count"),
        "comments_count": _count(payload, "comments_count"),
        "likes_count": _count(payload, "likes_count"),
        "link_card": _project_link(payload.get("link_card"), prefix),
        "media": _project_payload_media(payload.get("media"), prefix),
    }


def _project_post_payload(payload: dict, prefix: str = "") -> dict:
    pin_order = payload.get("pin_order")
    result = {
        "bid": _text(payload, "bid"),
        "uid": _text(payload, "uid"),
        "text": normalize_archive_text(_text(payload, "text")),
        "created_at": format_archive_time(_text(payload, "created_at")),
        "source": _text(payload, "source"),
        "ip_location": _text(payload, "ip_location"),
        "is_pinned": payload.get("is_pinned") is True,
        "pin_order": pin_order if isinstance(pin_order, int) else None,
        "visibility": _text(payload, "visibility") or "visible",
        "reposts_count": _count(payload, "reposts_count"),
        "comments_count": _count(payload, "comments_count"),
        "likes_count": _count(payload, "likes_count"),
        "retweeted_payload": _project_retweeted(payload.get("retweeted_payload"), prefix),
        "link_card_payload": _project_link(payload.get("link_card_payload"), prefix),
    }
    result["media"] = _project_payload_media(payload.get("media_signature"), prefix)
    return result


def _project_comment(payload: dict) -> dict:
    return {
        "text": normalize_archive_text(_text(payload, "text")),
        "user_name": normalize_archive_text(_text(payload, "user_name")),
        "user_id": _text(payload, "user_id"),
        "created_at": format_archive_time(_text(payload, "created_at")),
        "like_counts": _count(payload, "like_counts"),
        "is_blogger": payload.get("is_blogger") is True,
        "reply_to": _text(payload, "reply_to"),
        "source": _text(payload, "source"),
        "parent_id": _text(payload, "parent_id"),
        "reply_to_name": _text(payload, "reply_to_name"),
    }


@dataclass(frozen=True)
class ArchiveRenderSnapshot:
    schema: int
    user: dict
    posts: tuple[dict, ...]
    timeline: dict
    following: dict | None
    avatar_history: tuple = ()
    cover_history: tuple = ()
    pin_history: tuple = ()
    fans_history: tuple = ()

    @classmethod
    def from_repository(cls, repository: ArchiveRepository, progress_callback=None) -> "ArchiveRenderSnapshot":
        prefix = repository.paths.browser_url_prefix
        from .supertopic import STATE_KEY
        topic_state = repository._read_meta(STATE_KEY)
        topic_state = topic_state if isinstance(topic_state, dict) else {}
        topic_names = {x["id"]: x["name"] for x in topic_state.get("sources", [])
                       if isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("name"), str)}
        topic_origins = topic_state.get("post_sources", {})
        topic_origins = topic_origins if isinstance(topic_origins, dict) else {}
        exclusive_state = topic_state.get("exclusive_bids", [])
        exclusive_bids = ({bid for bid in exclusive_state if isinstance(bid, str)}
                          if isinstance(exclusive_state, list) else set())
        posts = repository.list_posts_for_render()
        profile_assets = repository.list_profile_assets()
        pin_observations = repository.list_pin_observations()
        current_pin_bids = (
            repository.latest_pin_bids() if pin_observations else None
        )
        comments = repository.list_comments_for_render()
        media = repository.list_media_for_render()
        revisions_by_post = repository.list_revisions_for_render()
        comments_by_post: dict[str, list[dict]] = {}
        media_by_owner: dict[tuple[str, str], list] = {}
        for item in media:
            _safe_local_path(item.local_path)
            media_by_owner.setdefault((item.owner_type, item.owner_id), []).append(item)
        def avatar_for(owner_type: str, owner_id: str) -> str:
            for item in media_by_owner.get((owner_type, owner_id), []):
                if item.role == "avatar":
                    return _browser_url(item.local_path, prefix)
            return ""
        for comment in comments:
            payload = _project_comment(comment.payload)
            payload.update({"id": comment.id, "parent_id": comment.parent_id, "captured_at": comment.captured_at})
            payload["avatar_url"] = avatar_for(
                "comment", payload["user_id"] or comment.id
            )
            payload["media"] = [
                {"kind": item.role, "position": item.position,
                 "local_path": _safe_local_path(item.local_path),
                 "browser_url": _browser_url(item.local_path, prefix)}
                for item in media_by_owner.get(("comment", comment.id), [])
            ]
            comments_by_post.setdefault(comment.post_bid, []).append(payload)

        rendered: list[tuple[int, dict]] = []
        if progress_callback is not None:
            progress_callback(0, len(posts))
        for stable_index, post in enumerate(posts):
            raw = _project_post_payload(asdict(post), prefix)
            origins = topic_origins.get(post.bid, [])
            if isinstance(origins, list):
                topics = [{"id": key, "name": topic_names[key]} for key in origins if isinstance(key, str) and key in topic_names]
                if topics:
                    raw["supertopics"] = topics
            if post.bid in exclusive_bids:
                raw["supertopic_exclusive"] = True
            if current_pin_bids is not None:
                raw["is_pinned"] = post.bid in current_pin_bids
            if raw["retweeted_payload"] is not None:
                raw["retweeted_payload"]["avatar_url"] = avatar_for(
                    "retweeted_user", raw["retweeted_payload"]["uid"]
                )
            raw["comments"] = comments_by_post.get(post.bid, [])
            post_media = media_by_owner.get(("post", post.bid), [])
            # 提取层历史版本可能把同一视频声明两次（URL 升级后绕过去重）；
            # 内容哈希相同的视频只展示一份，存量档案重渲染即修复。
            seen_video_sha: set[str] = set()
            deduped_media = []
            for item in post_media:
                if item.role == "video":
                    key = item.sha256 or item.local_path
                    if key in seen_video_sha:
                        continue
                    seen_video_sha.add(key)
                deduped_media.append(item)
            post_media = deduped_media
            if sum(item.get("type") == "video" for item in post.media_signature) == 1:
                # 旧补抓曾将同一视频写入不同位置；只展示单视频声明的最高画质副本。
                videos = [item for item in post_media if item.role == "video"]
                if len(videos) > 1:
                    best = max(videos, key=lambda item: video_quality_score(item.remote_url))
                    post_media = [item for item in post_media
                                  if item.role != "video" or item is best]
            raw["media"] = _post_media(post_media, prefix)
            link_payload = raw.get("link_card_payload")
            if isinstance(link_payload, dict) and link_payload.get("browser_url"):
                for item in raw["media"]:
                    if isinstance(item, dict) and item.get("hls") and not item["cover_url"]:
                        # 直播回放视频用直播卡片的本地封面做海报
                        item["cover_url"] = link_payload["browser_url"]
                        item["cover_path"] = link_payload.get("local_image", "")
            raw["revisions"] = [
                {
                    "revision_no": revision.revision_no,
                    "captured_at": revision.captured_at,
                    "payload": _project_post_payload(revision.payload, prefix),
                }
                for revision in revisions_by_post.get(post.bid, [])
            ]
            rendered.append((stable_index, raw))
            if progress_callback is not None and ((stable_index + 1) % 100 == 0 or stable_index + 1 == len(posts)):
                progress_callback(stable_index + 1, len(posts))

        def sort_key(item):
            stable_index, post = item
            if post["is_pinned"]:
                order = post["pin_order"]
                return (0, order is None, order if order is not None else 0, stable_index)
            parsed = _parse_time(post["created_at"])
            timestamp = parsed.timestamp() if parsed is not None else float("-inf")
            return (1, -timestamp, stable_index, post["bid"])

        rendered.sort(key=sort_key)
        ordered_posts = [item[1] for item in rendered]
        # 时间目录只覆盖主流微博；超话独占微博在互动页单独页签展示，索引序号与前端过滤后保持一致。
        timeline = _build_timeline_index(
            [post for post in ordered_posts if post.get("supertopic_exclusive") is not True]
        )
        manifest = repository.manifest()
        avatar_history = _asset_history(profile_assets, "avatar", prefix, repository.get_current_profile_asset("avatar"))
        cover_history = _asset_history(profile_assets, "cover", prefix, repository.get_current_profile_asset("cover"))
        pin_batches: list[dict] = []
        for observation in pin_observations:
            if pin_batches and pin_batches[-1]["observed_at"] == observation.observed_at:
                pin_batches[-1]["bids"].append(observation.bid)
            else:
                pin_batches.append(
                    {"observed_at": observation.observed_at, "bids": [observation.bid]}
                )
        pin_history: list[dict] = []
        for batch in pin_batches:
            if pin_history and set(pin_history[-1]["bids"]) == set(batch["bids"]):
                # 与上一次备份的置顶相同则不重复记录，只保留有变化的分批
                continue
            pin_history.append(batch)
        fans_history: list[dict] = []
        for observation in repository.list_fans_observations():
            if (
                fans_history
                and fans_history[-1]["followers_count"] == observation.followers_count
            ):
                # 与上一次备份数值相同则不重复展示，保留最新一条观察时间
                continue
            fans_history.append(
                {
                    "observed_at": observation.observed_at,
                    "followers_count": observation.followers_count,
                }
            )
        return cls(
            schema=1,
            user=_freeze({
                "uid": manifest.uid,
                "screen_name": manifest.screen_name,
                "created_at": manifest.created_at,
                "last_successful_sync_at": manifest.last_successful_sync_at,
                "avatar_url": next((item["url"] for item in avatar_history if item["current"]), avatar_for("user", manifest.uid)),
                "cover_url": next((item["url"] for item in cover_history if item["current"]), ""),
                "followers_count": (
                    fans_history[0]["followers_count"] if fans_history else None
                ),
            }),
            posts=tuple(_freeze(post) for post in ordered_posts),
            timeline=_freeze(timeline),
            following=_project_following(repository),
            avatar_history=_freeze(avatar_history),
            cover_history=_freeze(cover_history),
            pin_history=_freeze(pin_history),
            fans_history=_freeze(fans_history),
        )

    def payload(self) -> dict:
        return {
            "schema": self.schema,
            "format_version": BOOK_FORMAT_VERSION,
            "user": self.user,
            "posts": list(self.posts),
            "timeline": self.timeline,
            "following": self.following,
            "avatar_history": self.avatar_history,
            "cover_history": self.cover_history,
            "pin_history": self.pin_history,
            "fans_history": self.fans_history,
        }


def _asset_history(assets, kind: str, prefix: str, current_url: str = "") -> list[dict]:
    """旧行按图片身份合并，保留最早时间；当前标记只依据资料接口记录。"""
    grouped = {}
    for asset in sorted(assets, key=lambda item: item.first_seen_at):
        if asset.kind != kind:
            continue
        identity = profile_asset_identity(asset.remote_url)
        item = grouped.setdefault(identity, {
            "url": "", "first_seen_at": asset.first_seen_at,
            "current": bool(current_url) and identity == profile_asset_identity(current_url),
        })
        if not item["url"] and asset.local_path:
            item["url"] = _browser_url(_safe_local_path(asset.local_path), prefix)
    return sorted((item for item in grouped.values() if item["url"]),
                  key=lambda item: item["first_seen_at"], reverse=True)


def _project_following(repository: ArchiveRepository) -> dict | None:
    snapshot = repository.get_current_following_snapshot()
    if snapshot is None:
        return None
    items = repository.list_following_snapshot_items(snapshot.snapshot_id)
    relationships = repository.list_following_relationships()
    names = repository.list_following_names()
    changes = repository.list_following_changes()
    changes_by_snapshot: dict[str, dict[str, object]] = {}
    for change in changes:
        group = changes_by_snapshot.setdefault(
            change.snapshot_id,
            {
                "snapshot_id": change.snapshot_id,
                "followed": 0,
                "unfollowed": 0,
                "renamed": 0,
                "refollowed": 0,
            },
        )
        if change.change_type in (
            "followed",
            "unfollowed",
            "renamed",
            "refollowed",
        ):
            group[change.change_type] = int(group[change.change_type]) + 1
    return _freeze({
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "status": snapshot.status,
            "started_at": snapshot.started_at,
            "cutoff_at": snapshot.cutoff_at,
            "bloggers_complete": snapshot.bloggers_complete,
            "supertopics_complete": snapshot.supertopics_complete,
            "blogger_count": snapshot.blogger_reported_total or 0,
            "supertopic_count": snapshot.supertopic_reported_total or 0,
            "completed_at": snapshot.completed_at,
            "blogger_unconfirmed": bool(
                snapshot.summary.get("blogger_unconfirmed")
            ),
            "unconfirmed_bloggers": _freeze(
                snapshot.summary.get("unconfirmed_bloggers", [])
            ),
        },
        "items": [
            {
                "object_type": item.object_type,
                "object_id": item.object_id,
                "display_name": item.display_name,
                "page_url": item.page_url,
                "app_scheme": item.app_scheme,
                "source_order": item.source_order,
                "platform_followed_at": item.platform_followed_at,
            }
            for item in items
        ],
        "relationships": [
            {
                "object_type": rel.object_type,
                "object_id": rel.object_id,
                "active": rel.ended_snapshot_id is None,
                "local_first_seen_at": rel.local_first_seen_at,
                "last_confirmed_at": rel.last_confirmed_at,
                "platform_followed_at": rel.platform_followed_at,
            }
            for rel in relationships
        ],
        "names": [
            {
                "object_type": name.object_type,
                "object_id": name.object_id,
                "name": name.name,
                "current": name.ended_snapshot_id is None,
                "first_seen_at": name.first_seen_at,
                "last_seen_at": name.last_seen_at,
            }
            for name in names
        ],
        "changes": list(changes_by_snapshot.values()),
    })


def _post_media(records, prefix: str = "") -> list[dict]:
    grouped: dict[int, dict[str, object]] = {}
    for record in records:
        if record.role == "live_replay_segment":
            # 直播回放的 ts 分段只用于防止无引用清理，不参与渲染
            continue
        grouped.setdefault(record.position, {})[record.role] = record
    result = []
    for position in sorted(grouped):
        roles = grouped[position]
        if "live_replay" in roles:
            replay = roles["live_replay"]
            result.append({
                "kind": "video", "position": position, "hls": True,
                "local_path": _safe_local_path(replay.local_path), "browser_url": _browser_url(replay.local_path, prefix),
                "cover_path": "", "cover_url": "",
            })
        elif "live_photo" in roles and "live_photo_thumbnail" in roles:
            video = roles["live_photo"]
            image = roles["live_photo_thumbnail"]
            result.append({
                "kind": "live_photo", "position": position,
                "image_path": _safe_local_path(image.local_path), "image_url": _browser_url(image.local_path, prefix),
                "video_path": _safe_local_path(video.local_path), "video_url": _browser_url(video.local_path, prefix),
            })
        elif "live_photo" in roles:
            video = roles["live_photo"]
            result.append({
                "kind": "video", "position": position,
                "local_path": _safe_local_path(video.local_path), "browser_url": _browser_url(video.local_path, prefix), "cover_path": "", "cover_url": "",
            })
        elif "live_photo_thumbnail" in roles:
            image = roles["live_photo_thumbnail"]
            result.append({
                "kind": "image", "position": position,
                "local_path": _safe_local_path(image.local_path), "browser_url": _browser_url(image.local_path, prefix),
            })
        elif "video" in roles:
            video = roles["video"]
            cover = roles.get("video_cover") or roles.get("video_thumbnail")
            result.append({
                "kind": "video", "position": position,
                "local_path": _safe_local_path(video.local_path), "browser_url": _browser_url(video.local_path, prefix),
                "cover_path": _safe_local_path(cover.local_path) if cover else "",
                "cover_url": _browser_url(cover.local_path, prefix) if cover else "",
            })
        elif "image" in roles:
            image = roles["image"]
            thumbnail = roles.get("image_thumbnail")
            result.append({
                "kind": "image", "position": position,
                "local_path": _safe_local_path(image.local_path),
                "browser_url": _browser_url(image.local_path, prefix),
                "thumbnail_path": _safe_local_path(thumbnail.local_path) if thumbnail else "",
                "thumbnail_url": _browser_url(thumbnail.local_path, prefix) if thumbnail else "",
            })
    return result


def _snapshot_needs_hls(snapshot: ArchiveRenderSnapshot) -> bool:
    """快照里是否有 HLS 直播回放媒体（决定是否在 HTML 里引入 hls.js）。"""
    for post in snapshot.posts:
        media = post.get("media") if isinstance(post, dict) else None
        if isinstance(media, (list, tuple)) and any(
            isinstance(item, dict) and item.get("hls") for item in media
        ):
            return True
    return False


class ArchiveRenderer:
    def __init__(self, repository: ArchiveRepository):
        self.repository = repository
        self._progress_callback = None
        from .performance import PerformanceTimings
        self._timings = PerformanceTimings()
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(("html",)),
        )

    def _progress(self, subphase, detail, current=0, total=None, unit="file"):
        if self._progress_callback is not None:
            self._progress_callback({
                "phase": "generate", "subphase": subphase, "pct": 0.96,
                "detail": detail, "current": current, "total": total, "unit": unit,
                "timings_seconds": self._timings.snapshot(),
            })

    def _render_html(self, snapshot: ArchiveRenderSnapshot) -> str:
        hls_script_src = None
        if _snapshot_needs_hls(snapshot):
            data_script_src = self.repository.paths.data_script_src
            hls_script_src = "/".join(
                [*data_script_src.split("/")[:-1], HLS_LIBRARY_NAME]
            )
        return self.env.get_template("book_interactive.html").render(
            archive_mode=True,
            snapshot=snapshot,
            archive_data_src=self.repository.paths.data_script_src,
            hls_script_src=hls_script_src,
            book_format_version=BOOK_FORMAT_VERSION,
        )

    def _render_print_html(self, snapshot: ArchiveRenderSnapshot) -> str:
        return self.env.get_template("book.html").render(
            archive_mode=True, snapshot=snapshot
        )

    @staticmethod
    def _install_hls_library(target: Path) -> None:
        """把随包 hls.js 复制到归档数据目录（内容固定，存在且一致则跳过）。"""
        source = Path(__file__).resolve().parents[1] / "templates" / HLS_LIBRARY_NAME
        try:
            marker = target.lstat()
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
                raise ArchiveError("hls.js 安装目标不安全")
            if marker.st_size == source.stat().st_size:
                return
        except FileNotFoundError:
            pass
        temporary = target.with_name(f".{HLS_LIBRARY_NAME}.{secrets.token_hex(4)}.tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(block)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _render_markdown(snapshot: ArchiveRenderSnapshot) -> str:
        from ..generator import render_archive_markdown

        return render_archive_markdown(snapshot)

    @staticmethod
    def _data_source(snapshot: ArchiveRenderSnapshot) -> str:
        return ArchiveRenderer._encode_data_source(snapshot.payload())

    @staticmethod
    def _encode_data_source(payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        return f"window.__WEISHUSHU_ARCHIVE__ = {encoded};\n"

    @classmethod
    def _print_data_source(
        cls,
        snapshot: ArchiveRenderSnapshot,
        layout: ArchivePaths,
        stage: Path,
        cancel_requested: Callable[[], bool] | None,
    ) -> str:
        """为 PDF 生成本地缩略图数据，避免 Chromium 解码全部原图。"""
        from PIL import Image, ImageOps

        prefix = layout.browser_url_prefix
        media_base = layout.assets_dir
        payload = json.loads(json.dumps(snapshot.payload(), ensure_ascii=False))
        thumbnail_dir = stage / "print-media"
        thumbnail_dir.mkdir()
        cache: dict[str, str] = {}

        def thumbnail_url(browser_url: object) -> str:
            _check_cancelled(cancel_requested)
            if not isinstance(browser_url, str) or not browser_url:
                return ""
            if browser_url in cache:
                return cache[browser_url]
            try:
                raw = unquote(browser_url)
                if prefix and raw.startswith(prefix):
                    raw = raw[len(prefix):]
                local_path = _safe_local_path(raw)
                digest = hashlib.sha256(local_path.encode("utf-8")).hexdigest()[:24]
                target = thumbnail_dir / f"{digest}.jpg"
                with _open_archive_media(media_base, local_path) as stream:
                    with Image.open(stream) as opened:
                        image = ImageOps.exif_transpose(opened)
                        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
                        if image.mode != "RGB":
                            image = image.convert("RGB")
                        image.save(target, "JPEG", quality=82, optimize=True)
                result = target.resolve().as_uri()
            except UnsafeMediaPathError:
                raise
            except (ArchiveError, OSError, ValueError) as exc:
                logger.warning("PDF 缩略图生成失败: %s", exc)
                result = ""
            cache[browser_url] = result
            return result

        def project_link(card: object) -> None:
            if isinstance(card, dict):
                card["browser_url"] = thumbnail_url(card.get("browser_url"))

        def project_media(items: object) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind")
                if kind == "image":
                    source = item.get("thumbnail_url") or item.get("browser_url")
                    item["thumbnail_url"] = thumbnail_url(source)
                elif kind == "live_photo":
                    item["image_url"] = thumbnail_url(item.get("image_url"))
                elif kind == "video":
                    item["cover_url"] = thumbnail_url(item.get("cover_url"))

        user = payload.get("user")
        if isinstance(user, dict):
            user["avatar_url"] = thumbnail_url(user.get("avatar_url"))
        posts = payload.get("posts")
        if isinstance(posts, list):
            for post in posts:
                if not isinstance(post, dict):
                    continue
                project_media(post.get("media"))
                project_link(post.get("link_card_payload"))
                retweet = post.get("retweeted_payload")
                if isinstance(retweet, dict):
                    retweet["avatar_url"] = thumbnail_url(
                        retweet.get("avatar_url")
                    )
                    project_media(retweet.get("media"))
                    project_link(retweet.get("link_card"))
                comments = post.get("comments")
                if isinstance(comments, list):
                    for comment in comments:
                        if not isinstance(comment, dict):
                            continue
                        comment["avatar_url"] = thumbnail_url(
                            comment.get("avatar_url")
                        )
                        comment_media = comment.get("media")
                        if isinstance(comment_media, list):
                            for media in comment_media:
                                if isinstance(media, dict):
                                    media["browser_url"] = thumbnail_url(
                                        media.get("browser_url")
                                    )
        return cls._encode_data_source(payload)

    def render_all(
        self,
        root: Path,
        *,
        render_pdf: Callable[[Path, Path], object] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        begin_commit: Callable[[], bool] | None = None,
        include_pdf: bool = True,
        include_markdown: bool = True,
        progress_callback=None,
        timings=None,
    ) -> dict[str, Path]:
        self._progress_callback = progress_callback
        if timings is not None:
            self._timings = timings
        root = Path(root)
        repository_root = self.repository.root_path()
        if (
            root.is_symlink()
            or repository_root.is_symlink()
            or root.absolute() != repository_root.absolute()
        ):
            raise ArchiveError("渲染目录必须是当前归档根目录且不能是符号链接")
        layout = self.repository.paths
        data_dir = layout.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._validate_render_paths(root, data_dir, {})
        paths = {
            "html": layout.html_path,
            "data": layout.archive_data_path,
        }
        # PDF/Markdown 跳过时保持既有文件不动：不产出、不删除、不校验
        ordered: dict[str, Path] = {"html": paths["html"]}
        if include_pdf:
            ordered["pdf"] = layout.pdf_path
        if include_markdown:
            ordered["markdown"] = layout.markdown_path
        ordered["data"] = paths["data"]
        paths = ordered
        journal = layout.render_journal_path
        control_requested = _combined_control(cancel_requested, pause_requested)
        with self._render_lock(layout):
            _check_cancelled(control_requested)
            self._recover_render(journal, layout)
            self._cleanup_orphan_stages(data_dir)
            self._cleanup_restore_temps(layout)
            rendered = self._render_locked(
                root, layout, data_dir, paths, journal, render_pdf,
                control_requested, begin_commit, include_pdf, include_markdown,
            )
            self.repository._write_meta(BOOK_FORMAT_META_KEY, BOOK_FORMAT_VERSION)
            try:
                from .media_cleanup import cleanup_unreferenced_media

                with self._timings.measure("cleanup"):
                    self._progress("cleanup", "正在检查归档媒体引用", unit="media")
                    removed = cleanup_unreferenced_media(
                        layout,
                        self.repository.list_media_for_render(),
                        (
                            asset.local_path
                            for asset in self.repository.list_profile_assets()
                            if asset.local_path
                        ),
                        progress_callback=lambda current, total: self._progress(
                            "cleanup", "正在检查归档媒体引用", current, total, "media"
                        ),
                    )
                if removed:
                    logger.info("已清理 %d 个无引用媒体文件", len(removed))
            except ArchiveError as exc:
                logger.warning("归档已生成，但无引用媒体清理失败: %s", exc)
            return rendered

    def _render_locked(
        self, root, layout, data_dir, paths, journal, render_pdf,
        cancel_requested, begin_commit, include_pdf, include_markdown=True,
    ):
        _check_cancelled(cancel_requested)
        with self._timings.measure("generate"):
            self._progress("snapshot", "正在读取档案并整理页面数据", unit="post")
            snapshot = ArchiveRenderSnapshot.from_repository(
                self.repository, progress_callback=lambda current, total: self._progress(
                    "snapshot", "正在整理微博与媒体索引", current, total, "post"
                )
            )
        stage = Path(tempfile.mkdtemp(prefix=".render-stage-", dir=data_dir))
        staged = {
            "html": stage / "微博书.html",
            "print_html": stage / "微博书.print.html",
            "print_data": stage / "data" / "archive-print-data.js",
            "pdf": stage / "微博书.pdf",
            "markdown": stage / "微博书.md",
            "data": stage / "data" / "archive-data.js",
        }
        backup_names = {
            "html": "微博书.html", "pdf": "微博书.pdf",
            "markdown": "微博书.md", "data": "archive-data.js",
        }
        backups = {key: stage / "backup" / name for key, name in backup_names.items()}
        published: list[str] = []
        had_original = {key: target.exists() for key, target in paths.items()}
        state = {
            "stage": stage.name,
            "had_original": had_original, "phase": "staging", "published": [],
            "restored": [],
        }
        (stage / "backup").mkdir()
        cleanup = False
        self._write_journal(journal, state)
        try:
            if _snapshot_needs_hls(snapshot):
                # hls.js 是静态库，内容固定，不进原子发布集；回滚残留的
                # 副本无害（旧 HTML 不引用它），下次渲染会覆盖为同一内容。
                self._install_hls_library(paths["data"].parent / HLS_LIBRARY_NAME)
            contents = {}
            generators = [("html", "互动 HTML", self._render_html), ("data", "离线数据", self._data_source)]
            if include_markdown:
                generators.append(("markdown", "Markdown", self._render_markdown))
            file_total = len(paths)
            for index, (key, label, generate) in enumerate(generators):
                _check_cancelled(cancel_requested)
                with self._timings.measure("generate"):
                    self._progress(key, f"正在生成{label}", index, file_total)
                    contents[key] = generate(snapshot)
                    self._write_file(staged[key], contents[key].encode("utf-8"))
                    self._progress(key, f"{label}已生成", index + 1, file_total)
            with self._timings.measure("generate"):
                if include_pdf:
                    self._progress("pdf", "正在准备 PDF 排版与图片", len(generators), file_total)
                    contents["print_html"] = self._render_print_html(snapshot)
                    print_data = self._print_data_source(
                        snapshot, layout, stage, cancel_requested
                    )
                    self._write_file(staged["print_data"], print_data.encode("utf-8"))
                    print_source = self._print_html_source(
                        contents["print_html"], root.resolve(), staged["print_data"].resolve()
                    )
                    self._write_file(staged["print_html"], print_source.encode("utf-8"))
                    staged["pdf"].parent.mkdir(parents=True, exist_ok=True)
                    if render_pdf is None:
                        self._default_pdf(
                            staged["print_html"], staged["pdf"], root,
                            len(snapshot.posts), cancel_requested,
                            progress_callback=lambda current, total: self._progress(
                                "pdf", "正在生成 PDF 分批页面", current, total, "batch"
                            ),
                        )
                    else:
                        render_pdf(staged["print_html"], staged["pdf"])
                    _check_cancelled(cancel_requested)
                    if not staged["pdf"].is_file() or staged["pdf"].stat().st_size == 0:
                        raise ArchiveError("PDF 渲染未生成有效文件")
                    self._fsync_file(staged["pdf"])

            with self._timings.measure("publish"):
                self._progress("publish", "正在备份已有成品文件", 0, len(paths))
                state["phase"] = "backing_up"
                self._write_journal(journal, state)
                for key, target in paths.items():
                    if had_original[key]:
                        backups[key].parent.mkdir(parents=True, exist_ok=True)
                        self._copy_fsync(target, backups[key])
                self._fsync_directory(backups["html"].parent)
                self._fsync_directory(root)
                self._fsync_directory(data_dir)
                state["phase"] = "publishing"
                self._write_journal(journal, state)
                for index, (key, target) in enumerate(paths.items()):
                    self._progress("publish", "正在安全写入成品文件", index, len(paths))
                    _check_cancelled(cancel_requested)
                    self._validate_render_paths(root, data_dir, paths)
                    os.replace(staged[key], target)
                    published.append(key)
                    state["published"] = list(published)
                    self._write_journal(journal, state)
                    self._fsync_directory(target.parent)
                    self._progress("publish", "成品文件已写入", index + 1, len(paths))
            self._validate_render_paths(root, data_dir, paths)
            _check_cancelled(cancel_requested)
            if begin_commit is not None and not begin_commit():
                _check_cancelled(cancel_requested)
                raise OperationCancelled("任务已取消")
            state["phase"] = "committed"
            self._write_journal(journal, state)
            self._fsync_directory(root)
            self._fsync_directory(data_dir)
            from .readme import ensure_archive_readme
            ensure_archive_readme(root)
            cleanup = True
            return paths
        except BaseException:
            try:
                self._rollback_state(state, layout, journal)
            except BaseException:
                logger.exception("归档渲染发布回滚失败，已保留恢复状态")
                raise
            cleanup = True
            raise
        finally:
            if cleanup:
                self._remove_tree(stage)
                try:
                    journal.unlink()
                    self._fsync_directory(journal.parent)
                except FileNotFoundError:
                    pass

    @classmethod
    @contextmanager
    def _render_lock(cls, layout: ArchivePaths):
        lock_path = layout.render_lock_path
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            marker = os.fstat(descriptor)
            if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise ArchiveError("归档渲染锁文件必须是单链接普通文件")
            if os.name == "nt":
                import msvcrt
                os.write(descriptor, b"0") if os.fstat(descriptor).st_size == 0 else None
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @classmethod
    def _write_journal(cls, path: Path, payload: dict) -> None:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            try: temporary.unlink()
            except FileNotFoundError: pass

    @classmethod
    def _recover_render(cls, journal: Path, layout: ArchivePaths) -> None:
        if not journal.exists():
            return
        try:
            descriptor = os.open(journal, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                marker = os.fstat(stream.fileno())
                if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                    raise ArchiveError("归档渲染恢复状态文件不安全")
                state = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveError("归档渲染恢复状态损坏", original=exc) from exc
        required = {
            "stage", "had_original", "phase", "published", "restored",
        }
        keys = {"html", "pdf", "markdown", "data"}
        had_original = state.get("had_original") if isinstance(state, dict) else None
        if (
            not isinstance(state, dict)
            or set(state) != required
            or not isinstance(state.get("stage"), str)
            or not isinstance(had_original, dict)
            or not had_original
            or not set(had_original) <= keys
            or not all(isinstance(value, bool) for value in had_original.values())
            or state.get("phase") not in {"staging", "backing_up", "publishing", "committed"}
            or not isinstance(state.get("published"), list)
            or not isinstance(state.get("restored"), list)
            or any(value not in had_original for value in state["published"])
            or any(value not in had_original for value in state["restored"])
        ):
            raise ArchiveError("归档渲染恢复状态字段损坏")
        root = layout.root
        data_dir = layout.data_dir
        stage_name = state["stage"]
        stage = data_dir / stage_name
        expected_targets = {
            "html": layout.html_path, "pdf": layout.pdf_path,
            "markdown": layout.markdown_path, "data": layout.archive_data_path,
        }
        expected_backups = {
            "html": stage / "backup" / "微博书.html",
            "pdf": stage / "backup" / "微博书.pdf",
            "markdown": stage / "backup" / "微博书.md",
            "data": stage / "backup" / "archive-data.js",
        }
        if (
            Path(stage_name).name != stage_name
            or not stage_name.startswith(".render-stage-")
            or len(stage_name) <= len(".render-stage-")
        ):
            raise ArchiveError("归档渲染恢复状态路径不安全")
        cls._validate_render_paths(root, data_dir, expected_targets)
        cls._validate_stage_paths(stage, state, expected_backups)
        if state.get("phase") not in {"committed", "staging", "backing_up"}:
            cls._rollback_state(state, layout, journal)
        cls._cleanup_restore_temps(layout)
        cls._remove_tree(stage)
        journal.unlink()
        cls._fsync_directory(journal.parent)

    @classmethod
    def _rollback_state(cls, state: dict, layout: ArchivePaths, journal: Path) -> None:
        if state.get("phase") in {"staging", "backing_up"}:
            return
        stage = layout.data_dir / state["stage"]
        targets = {
            "html": layout.html_path, "pdf": layout.pdf_path,
            "markdown": layout.markdown_path, "data": layout.archive_data_path,
        }
        backups = {
            "html": stage / "backup" / "微博书.html",
            "pdf": stage / "backup" / "微博书.pdf",
            "markdown": stage / "backup" / "微博书.md",
            "data": stage / "backup" / "archive-data.js",
        }
        for key in state["had_original"]:
            target = targets[key]
            staged_source = stage / (
                "data/archive-data.js" if key == "data" else
                {"html": "微博书.html", "pdf": "微博书.pdf", "markdown": "微博书.md"}[key]
            )
            was_published = key in state.get("published", []) or not staged_source.exists()
            if state["had_original"].get(key):
                backup = backups[key]
                if not backup.exists():
                    raise ArchiveError("归档渲染备份缺失，无法安全恢复")
                cls._restore_backup(backup, target)
            elif was_published:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                cls._fsync_directory(target.parent)
            restored = state.setdefault("restored", [])
            if key not in restored:
                restored.append(key)
            cls._write_journal(journal, state)

    @classmethod
    def _restore_backup(cls, backup: Path, target: Path) -> None:
        if not _SUPPORTS_DIRECTORY_FDS:
            source_marker = backup.lstat()
            parent_marker = target.parent.lstat()
            if (
                stat.S_ISLNK(source_marker.st_mode)
                or not stat.S_ISREG(source_marker.st_mode)
                or source_marker.st_nlink != 1
            ):
                raise ArchiveError("归档渲染备份必须是单链接普通文件")
            if stat.S_ISLNK(parent_marker.st_mode) or not stat.S_ISDIR(parent_marker.st_mode):
                raise ArchiveError("归档渲染目标目录不安全")
            temporary = target.with_name(
                f".{target.name}.restore-{secrets.token_hex(8)}"
            )
            source_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            temporary_fd = -1
            try:
                opened = os.fstat(source_fd)
                current_source = backup.lstat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_mode)
                    != (current_source.st_dev, current_source.st_ino, current_source.st_mode)
                ):
                    raise ArchiveError("归档渲染备份在打开时已变化")
                temporary_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                while block := os.read(source_fd, 1024 * 1024):
                    view = memoryview(block)
                    while view:
                        written = os.write(temporary_fd, view)
                        view = view[written:]
                os.fsync(temporary_fd)
                os.close(temporary_fd)
                temporary_fd = -1
                current_parent = target.parent.lstat()
                if (
                    stat.S_ISLNK(current_parent.st_mode)
                    or not stat.S_ISDIR(current_parent.st_mode)
                    or (current_parent.st_dev, current_parent.st_ino, current_parent.st_mode)
                    != (parent_marker.st_dev, parent_marker.st_ino, parent_marker.st_mode)
                ):
                    raise ArchiveError("归档渲染目标目录在恢复时已变化")
                os.replace(temporary, target)
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                temporary.unlink(missing_ok=True)
                os.close(source_fd)
            return
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(backup, source_flags)
        parent_fd = os.open(target.parent, directory_flags)
        temporary_name = f".{target.name}.restore-{secrets.token_hex(8)}"
        temporary_fd = -1
        try:
            marker = os.fstat(source_fd)
            if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise ArchiveError("归档渲染备份必须是单链接普通文件")
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            while block := os.read(source_fd, 1024 * 1024):
                view = memoryview(block)
                while view:
                    written = os.write(temporary_fd, view)
                    view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = -1
            os.replace(
                temporary_name, target.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
            os.close(source_fd)

    @staticmethod
    def _validate_stage_paths(stage: Path, state: dict, backups: dict[str, Path]) -> None:
        try:
            stage_marker = os.lstat(stage)
            backup_marker = os.lstat(stage / "backup")
        except FileNotFoundError as exc:
            raise ArchiveError("归档渲染暂存目录缺失", original=exc) from exc
        if (
            stat.S_ISLNK(stage_marker.st_mode) or not stat.S_ISDIR(stage_marker.st_mode)
            or stat.S_ISLNK(backup_marker.st_mode) or not stat.S_ISDIR(backup_marker.st_mode)
        ):
            raise ArchiveError("归档渲染暂存路径不安全")
        try:
            staged_data_marker = os.lstat(stage / "data")
        except FileNotFoundError:
            staged_data_marker = None
        if staged_data_marker is not None and (
            stat.S_ISLNK(staged_data_marker.st_mode)
            or not stat.S_ISDIR(staged_data_marker.st_mode)
        ):
            raise ArchiveError("归档渲染暂存 data 路径不安全")
        staged_all = {
            "html": stage / "微博书.html", "pdf": stage / "微博书.pdf",
            "markdown": stage / "微博书.md", "data": stage / "data" / "archive-data.js",
        }
        staged = {key: staged_all[key] for key in state["had_original"]}
        backups = {key: value for key, value in backups.items() if key in state["had_original"]}
        for key, path in {**staged, **{f"backup:{key}": value for key, value in backups.items()}}.items():
            try:
                marker = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise ArchiveError(f"归档渲染暂存文件不安全: {key}")
        for key, had_original in state["had_original"].items():
            if had_original and state.get("phase") in {"publishing", "committed"} and not backups[key].exists():
                raise ArchiveError("归档渲染备份缺失，无法安全恢复")

    @classmethod
    def _cleanup_orphan_stages(cls, data_dir: Path) -> None:
        for stage in data_dir.glob(".render-stage-*"):
            marker = os.lstat(stage)
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
                raise ArchiveError("无主归档渲染暂存路径不安全")
            cls._remove_tree(stage)
        cls._fsync_directory(data_dir)

    @classmethod
    def _cleanup_restore_temps(cls, layout: ArchivePaths) -> None:
        root = layout.root
        patterns = (
            (root, ".微博书.html.restore-*"),
            (root, ".微博书.pdf.restore-*"),
            (root, ".微博书.md.restore-*"),
            (layout.data_dir, ".archive-data.js.restore-*"),
        )
        touched: set[Path] = set()
        for directory, pattern in patterns:
            for temporary in directory.glob(pattern):
                marker = os.lstat(temporary)
                if (
                    stat.S_ISLNK(marker.st_mode)
                    or not stat.S_ISREG(marker.st_mode)
                    or marker.st_nlink != 1
                ):
                    raise ArchiveError("归档恢复临时文件不安全")
                temporary.unlink()
                touched.add(directory)
        for directory in touched:
            cls._fsync_directory(directory)

    @staticmethod
    def _validate_render_paths(root: Path, data_dir: Path, paths: dict) -> None:
        for directory, label in ((root, "归档根目录"), (data_dir, "归档数据目录")):
            marker = os.lstat(directory)
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
                raise ArchiveError(f"{label}必须是真实目录")
        for target in paths.values():
            try:
                marker = os.lstat(target)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise ArchiveError("归档固定成品必须是单链接普通文件")

    @staticmethod
    def _write_file(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _copy_fsync(cls, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        destination_fd = -1
        try:
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            marker = os.fstat(source_fd)
            if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise ArchiveError("归档固定成品必须是单链接普通文件")
            while block := os.read(source_fd, 1024 * 1024):
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
            os.close(source_fd)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _print_html_source(source: str, archive_root: Path, data_path: Path) -> str:
        base_uri = escape(_as_file_uri(archive_root).rstrip("/") + "/", quote=True)
        data_uri = escape(_as_file_uri(data_path), quote=True)
        result = source.replace("<head>", f'<head>\n<base href="{base_uri}">', 1)
        marker = '<script src="data/archive-data.js"></script>'
        if marker not in result:
            raise ArchiveError("打印页面缺少归档数据脚本入口")
        return result.replace(marker, f'<script src="{data_uri}"></script>', 1)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt" or not path.exists():
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_tree(root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        root.rmdir()

    @staticmethod
    def _default_pdf(
        html_path: Path,
        pdf_path: Path,
        archive_root: Path,
        post_count: int,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback=None,
    ) -> None:
        from pypdf import PdfWriter
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
        if post_count < 0:
            raise ArchiveError("PDF 微博数量不能为负数")
        batches = [
            (start, min(_PDF_PRINT_BATCH_SIZE, post_count - start))
            for start in range(0, post_count, _PDF_PRINT_BATCH_SIZE)
        ] or [(0, 0)]
        chunks = [
            pdf_path.with_name(f".{pdf_path.stem}.part-{index:04d}.pdf")
            for index in range(len(batches))
        ]
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for index, ((start, limit), chunk) in enumerate(zip(batches, chunks, strict=True)):
                        if progress_callback is not None:
                            progress_callback(index, len(batches))
                        _check_cancelled(cancel_requested)
                        page = browser.new_page()
                        failed_requests: list[str] = []
                        page.on(
                            "requestfailed",
                            lambda request, failed=failed_requests: failed.append(
                                request.url
                            ),
                        )
                        page.goto(
                            f"{html_path.resolve().as_uri()}?print=1"
                            f"&printStart={start}&printLimit={limit}",
                            wait_until="load",
                        )
                        page.wait_for_function(
                            "window.__WEISHUSHU_PRINT_READY__ === true"
                        )
                        page.evaluate("() => document.fonts.ready")
                        try:
                            page.wait_for_function(
                                "() => Array.from(document.images)"
                                ".filter(image => image.hasAttribute('src')).every(image => "
                                "image.complete && image.naturalWidth > 0)",
                                timeout=15000,
                            )
                        except PlaywrightTimeoutError as exc:
                            broken = page.eval_on_selector_all(
                                "img",
                                "images => images.filter(image => image.hasAttribute('src') && "
                                "(!image.complete || image.naturalWidth === 0))"
                                ".map(image => image.getAttribute('src') || '')",
                            )
                            logger.error("PDF 本地图片加载失败: %s", broken)
                            raise ArchiveError("PDF 本地图片加载失败") from exc
                        if failed_requests:
                            logger.error(
                                "PDF 本地资源请求失败: %s", failed_requests
                            )
                            raise ArchiveError("PDF 本地资源请求失败")
                        page.pdf(
                            path=str(chunk), format="A4", print_background=True
                        )
                        _check_cancelled(cancel_requested)
                        page.close()
                        ArchiveRenderer._make_pdf_links_portable(
                            chunk, archive_root
                        )
                        if progress_callback is not None:
                            progress_callback(index + 1, len(batches))
                finally:
                    browser.close()

            writer = PdfWriter()
            for chunk in chunks:
                _check_cancelled(cancel_requested)
                writer.append(str(chunk))
            _check_cancelled(cancel_requested)
            with pdf_path.open("wb") as stream:
                writer.write(stream)
            writer.close()
        finally:
            for chunk in chunks:
                try:
                    chunk.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _make_pdf_links_portable(pdf_path: Path, archive_root: Path) -> None:
        """把 Chromium 写入 PDF 的本机绝对 HTML 链接改为等长相对 URI。

        Chromium 会先用打印页的 ``<base>`` 解析 ``微博书.html``，再把
        ``file:///...`` 绝对地址写入 PDF 注解。直接改变 PDF 字节长度会使
        xref 偏移失效，因此用 PDF 链接的无副作用查询参数填充为等长。
        整个档案搬移后，PDF 查看器会以 PDF 所在目录解析该相对 URI。
        """
        content = pdf_path.read_bytes()
        absolute = (archive_root.resolve() / "微博书.html").as_uri().encode("ascii")
        relative = quote("微博书.html", safe="-._~").encode("ascii")
        query = b"?archive="
        padding_size = len(absolute) - len(relative) - len(query)
        if padding_size < 0:
            raise ArchiveError("PDF 相对链接长度异常")

        marker = b"/URI (" + absolute + b"#post-"
        offset = 0
        rewritten = bytearray(content)
        replacements = 0
        while True:
            found = content.find(marker, offset)
            if found < 0:
                break
            uri_start = found + len(b"/URI (")
            uri_end = content.find(b")", uri_start)
            if uri_end < 0:
                raise ArchiveError("PDF 媒体链接结构损坏")
            fragment = content[uri_start + len(absolute):uri_end]
            replacement = relative + query + (b"0" * padding_size) + fragment
            if len(replacement) != uri_end - uri_start:
                raise ArchiveError("PDF 相对链接长度校验失败")
            rewritten[uri_start:uri_end] = replacement
            replacements += 1
            offset = uri_end + 1
        if replacements:
            pdf_path.write_bytes(rewritten)
