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
from .presentation import format_archive_time, normalize_archive_text
from .repository import ArchiveError, ArchiveRepository

logger = logging.getLogger(__name__)
_PDF_PRINT_BATCH_SIZE = 50
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
def _open_archive_media(archive_root: Path, local_path: str):
    """在归档根目录内打开非链接普通媒体文件。"""
    physical_root = archive_root.resolve(strict=True)
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


def _browser_url(value: str) -> str:
    path = _safe_local_path(value)
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


def _project_link(payload: object) -> dict | None:
    if not isinstance(payload, dict):
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
        "browser_url": _browser_url(_local(payload, "local_image"))
        if _local(payload, "local_image") else "",
    }
    return result


def _project_payload_media(payload: object) -> list[dict]:
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
            result.append({"kind": "image", "position": position, "local_path": local_path, "browser_url": _browser_url(local_path)})
        elif kind == "video" and local_path:
            result.append({
                "kind": "video", "position": position,
                "local_path": local_path, "browser_url": _browser_url(local_path),
                "cover_path": video_cover or local_thumb,
                "cover_url": _browser_url(video_cover or local_thumb) if video_cover or local_thumb else "",
            })
        elif kind == "live_photo" and local_path and local_thumb:
            result.append({
                "kind": "live_photo", "position": position,
                "video_path": local_path, "video_url": _browser_url(local_path),
                "image_path": local_thumb, "image_url": _browser_url(local_thumb),
            })
        elif kind == "live_photo" and local_path:
            result.append({
                "kind": "video", "position": position,
                "local_path": local_path, "browser_url": _browser_url(local_path),
                "cover_path": video_cover, "cover_url": _browser_url(video_cover) if video_cover else "",
            })
        elif kind == "live_photo" and local_thumb:
            result.append({"kind": "image", "position": position, "local_path": local_thumb, "browser_url": _browser_url(local_thumb)})
        elif kind:
            result.append({"kind": "unavailable", "position": position})
    return result


def _project_retweeted(payload: object) -> dict | None:
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
        "link_card": _project_link(payload.get("link_card")),
        "media": _project_payload_media(payload.get("media")),
    }


def _project_post_payload(payload: dict) -> dict:
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
        "retweeted_payload": _project_retweeted(payload.get("retweeted_payload")),
        "link_card_payload": _project_link(payload.get("link_card_payload")),
    }
    result["media"] = _project_payload_media(payload.get("media_signature"))
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

    @classmethod
    def from_repository(cls, repository: ArchiveRepository) -> "ArchiveRenderSnapshot":
        posts = repository.list_posts_for_render()
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
                    return _browser_url(item.local_path)
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
                 "browser_url": _browser_url(item.local_path)}
                for item in media_by_owner.get(("comment", comment.id), [])
            ]
            comments_by_post.setdefault(comment.post_bid, []).append(payload)

        rendered: list[tuple[int, dict]] = []
        for stable_index, post in enumerate(posts):
            raw = _project_post_payload(asdict(post))
            if raw["retweeted_payload"] is not None:
                raw["retweeted_payload"]["avatar_url"] = avatar_for(
                    "retweeted_user", raw["retweeted_payload"]["uid"]
                )
            raw["comments"] = comments_by_post.get(post.bid, [])
            raw["media"] = _post_media(media_by_owner.get(("post", post.bid), []))
            raw["revisions"] = [
                {
                    "revision_no": revision.revision_no,
                    "captured_at": revision.captured_at,
                    "payload": _project_post_payload(revision.payload),
                }
                for revision in revisions_by_post.get(post.bid, [])
            ]
            rendered.append((stable_index, raw))

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
        timeline = _build_timeline_index(ordered_posts)
        manifest = repository.manifest()
        return cls(
            schema=1,
            user=_freeze({
                "uid": manifest.uid,
                "screen_name": manifest.screen_name,
                "created_at": manifest.created_at,
                "last_successful_sync_at": manifest.last_successful_sync_at,
                "avatar_url": avatar_for("user", manifest.uid),
            }),
            posts=tuple(_freeze(post) for post in ordered_posts),
            timeline=_freeze(timeline),
            following=_project_following(repository),
        )

    def payload(self) -> dict:
        return {
            "schema": self.schema,
            "user": self.user,
            "posts": list(self.posts),
            "timeline": self.timeline,
            "following": self.following,
        }


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


def _post_media(records) -> list[dict]:
    grouped: dict[int, dict[str, object]] = {}
    for record in records:
        grouped.setdefault(record.position, {})[record.role] = record
    result = []
    for position in sorted(grouped):
        roles = grouped[position]
        if "live_photo" in roles and "live_photo_thumbnail" in roles:
            video = roles["live_photo"]
            image = roles["live_photo_thumbnail"]
            result.append({
                "kind": "live_photo", "position": position,
                "image_path": _safe_local_path(image.local_path), "image_url": _browser_url(image.local_path),
                "video_path": _safe_local_path(video.local_path), "video_url": _browser_url(video.local_path),
            })
        elif "live_photo" in roles:
            video = roles["live_photo"]
            result.append({
                "kind": "video", "position": position,
                "local_path": _safe_local_path(video.local_path), "browser_url": _browser_url(video.local_path), "cover_path": "", "cover_url": "",
            })
        elif "live_photo_thumbnail" in roles:
            image = roles["live_photo_thumbnail"]
            result.append({
                "kind": "image", "position": position,
                "local_path": _safe_local_path(image.local_path), "browser_url": _browser_url(image.local_path),
            })
        elif "video" in roles:
            video = roles["video"]
            cover = roles.get("video_cover") or roles.get("video_thumbnail")
            result.append({
                "kind": "video", "position": position,
                "local_path": _safe_local_path(video.local_path), "browser_url": _browser_url(video.local_path),
                "cover_path": _safe_local_path(cover.local_path) if cover else "",
                "cover_url": _browser_url(cover.local_path) if cover else "",
            })
        elif "image" in roles:
            image = roles["image"]
            thumbnail = roles.get("image_thumbnail")
            result.append({
                "kind": "image", "position": position,
                "local_path": _safe_local_path(image.local_path),
                "browser_url": _browser_url(image.local_path),
                "thumbnail_path": _safe_local_path(thumbnail.local_path) if thumbnail else "",
                "thumbnail_url": _browser_url(thumbnail.local_path) if thumbnail else "",
            })
    return result


class ArchiveRenderer:
    def __init__(self, repository: ArchiveRepository):
        self.repository = repository
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(("html",)),
        )

    def _render_html(self, snapshot: ArchiveRenderSnapshot) -> str:
        return self.env.get_template("book_interactive.html").render(
            archive_mode=True, snapshot=snapshot
        )

    def _render_print_html(self, snapshot: ArchiveRenderSnapshot) -> str:
        return self.env.get_template("book.html").render(
            archive_mode=True, snapshot=snapshot
        )

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
        archive_root: Path,
        stage: Path,
        cancel_requested: Callable[[], bool] | None,
    ) -> str:
        """为 PDF 生成本地缩略图数据，避免 Chromium 解码全部原图。"""
        from PIL import Image, ImageOps

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
                local_path = _safe_local_path(unquote(browser_url))
                digest = hashlib.sha256(local_path.encode("utf-8")).hexdigest()[:24]
                target = thumbnail_dir / f"{digest}.jpg"
                with _open_archive_media(archive_root, local_path) as stream:
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
    ) -> dict[str, Path]:
        root = Path(root)
        repository_root = self.repository.root_path()
        if (
            root.is_symlink()
            or repository_root.is_symlink()
            or root.absolute() != repository_root.absolute()
        ):
            raise ArchiveError("渲染目录必须是当前归档根目录且不能是符号链接")
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._validate_render_paths(root, data_dir, {})
        paths = {
            "html": root / "微博书.html", "pdf": root / "微博书.pdf",
            "markdown": root / "微博书.md", "data": data_dir / "archive-data.js",
        }
        journal = self._journal_path(root)
        control_requested = _combined_control(cancel_requested, pause_requested)
        with self._render_lock(root):
            _check_cancelled(control_requested)
            self._recover_render(journal, root)
            self._cleanup_orphan_stages(data_dir)
            self._cleanup_restore_temps(root)
            rendered = self._render_locked(
                root, data_dir, paths, journal, render_pdf, control_requested,
                begin_commit,
            )
            try:
                from .media_cleanup import cleanup_unreferenced_media

                removed = cleanup_unreferenced_media(
                    root, self.repository.list_media_for_render()
                )
                if removed:
                    logger.info("已清理 %d 个无引用媒体文件", len(removed))
            except ArchiveError as exc:
                logger.warning("归档已生成，但无引用媒体清理失败: %s", exc)
            return rendered

    def _render_locked(
        self, root, data_dir, paths, journal, render_pdf, cancel_requested,
        begin_commit,
    ):
        _check_cancelled(cancel_requested)
        snapshot = ArchiveRenderSnapshot.from_repository(self.repository)
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
            contents = {
                "html": self._render_html(snapshot),
                "print_html": self._render_print_html(snapshot),
                "markdown": self._render_markdown(snapshot),
                "data": self._data_source(snapshot),
            }
            for key in ("html", "markdown", "data"):
                self._write_file(staged[key], contents[key].encode("utf-8"))
            print_data = self._print_data_source(
                snapshot, root, stage, cancel_requested
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
                )
            else:
                render_pdf(staged["print_html"], staged["pdf"])
            _check_cancelled(cancel_requested)
            if not staged["pdf"].is_file() or staged["pdf"].stat().st_size == 0:
                raise ArchiveError("PDF 渲染未生成有效文件")
            self._fsync_file(staged["pdf"])

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
            for key, target in paths.items():
                _check_cancelled(cancel_requested)
                self._validate_render_paths(root, data_dir, paths)
                os.replace(staged[key], target)
                published.append(key)
                state["published"] = list(published)
                self._write_journal(journal, state)
                self._fsync_directory(target.parent)
            self._validate_render_paths(root, data_dir, paths)
            _check_cancelled(cancel_requested)
            if begin_commit is not None and not begin_commit():
                _check_cancelled(cancel_requested)
                raise OperationCancelled("任务已取消")
            state["phase"] = "committed"
            self._write_journal(journal, state)
            self._fsync_directory(root)
            self._fsync_directory(data_dir)
            cleanup = True
            return paths
        except BaseException:
            try:
                self._rollback_state(state, root, journal)
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

    @staticmethod
    def _journal_path(root: Path) -> Path:
        return root / "data" / ".weishushu-render-state.json"

    @classmethod
    @contextmanager
    def _render_lock(cls, root: Path):
        lock_path = root / "data" / ".weishushu-render.lock"
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
    def _recover_render(cls, journal: Path, root: Path) -> None:
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
        if (
            not isinstance(state, dict)
            or set(state) != required
            or not isinstance(state.get("stage"), str)
            or not isinstance(state.get("had_original"), dict)
            or set(state["had_original"]) != keys
            or not all(isinstance(value, bool) for value in state["had_original"].values())
            or state.get("phase") not in {"staging", "backing_up", "publishing", "committed"}
            or not isinstance(state.get("published"), list)
            or not isinstance(state.get("restored"), list)
            or any(value not in keys for value in state["published"])
            or any(value not in keys for value in state["restored"])
        ):
            raise ArchiveError("归档渲染恢复状态字段损坏")
        data_dir = root / "data"
        stage_name = state["stage"]
        stage = data_dir / stage_name
        expected_targets = {
            "html": root / "微博书.html", "pdf": root / "微博书.pdf",
            "markdown": root / "微博书.md", "data": root / "data" / "archive-data.js",
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
            cls._rollback_state(state, root, journal)
        cls._cleanup_restore_temps(root)
        cls._remove_tree(stage)
        journal.unlink()
        cls._fsync_directory(journal.parent)

    @classmethod
    def _rollback_state(cls, state: dict, root: Path, journal: Path) -> None:
        if state.get("phase") in {"staging", "backing_up"}:
            return
        stage = root / "data" / state["stage"]
        targets = {
            "html": root / "微博书.html", "pdf": root / "微博书.pdf",
            "markdown": root / "微博书.md", "data": root / "data" / "archive-data.js",
        }
        backups = {
            "html": stage / "backup" / "微博书.html",
            "pdf": stage / "backup" / "微博书.pdf",
            "markdown": stage / "backup" / "微博书.md",
            "data": stage / "backup" / "archive-data.js",
        }
        for key, target in targets.items():
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
        staged = {
            "html": stage / "微博书.html", "pdf": stage / "微博书.pdf",
            "markdown": stage / "微博书.md", "data": stage / "data" / "archive-data.js",
        }
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
    def _cleanup_restore_temps(cls, root: Path) -> None:
        patterns = (
            (root, ".微博书.html.restore-*"),
            (root, ".微博书.pdf.restore-*"),
            (root, ".微博书.md.restore-*"),
            (root / "data", ".archive-data.js.restore-*"),
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
        for directory, label in ((root, "归档根目录"), (data_dir, "归档 data 目录")):
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
                    for (start, limit), chunk in zip(batches, chunks, strict=True):
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
