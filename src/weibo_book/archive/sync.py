"""当前登录账号的可携带微博书同步状态机。"""

from __future__ import annotations

import os
import errno
import hashlib
import json
import logging
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from weibo_book.errors import (
    OperationCancelled,
    OperationPaused,
    WeiboError,
    WeiboErrorKind,
    classify_error,
)
from weibo_book.models import Comment, LinkCard, Post, PostMedia

from .discovery import ProfileItem, ProfilePage, discover_incremental
from .media_layout import media_path_shape
from .repository import ArchiveRepository
from .schema import CommentRecord, MediaRecord, PostRecord
from .pacing import AdaptiveRequestScheduler


logger = logging.getLogger(__name__)

SyncMode = Literal["create", "incremental", "rebuild"]
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
# 增量更新时重新检查的旧微博条数（2026-08-24 由 50 调为 5，减少重复抓取开销）
_INCREMENTAL_REFRESH_LIMIT = 5
_SUPPORTS_DIRECTORY_FDS = (
    os.name != "nt"
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)


def _physical_root(root: Path) -> Path:
    """将归档标识绑定到父目录的物理路径。"""
    return root.parent.resolve(strict=True) / root.name


def _physical_named_path(path: Path) -> Path:
    return path.parent.resolve(strict=True) / path.name


def _archive_state_stem(root: Path) -> str:
    physical = _physical_root(root)
    digest = hashlib.sha256(str(physical).encode("utf-8")).hexdigest()[:16]
    return f".weishushu-{digest}"


def _archive_lock_path(root: Path) -> Path:
    physical = _physical_root(root)
    return physical.parent / f"{_archive_state_stem(physical)}.lock"


def _rebuild_state_path(root: Path) -> Path:
    physical = _physical_root(root)
    return physical.parent / f"{_archive_state_stem(physical)}.rebuild.json"


def _fsync_directory(path: Path) -> None:
    if not _SUPPORTS_DIRECTORY_FDS:
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | _O_DIRECTORY,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state_atomic(path: Path, payload: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise WeiboError("持久化重建恢复状态失败", original=exc) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _opened_matches(marker: os.stat_result, descriptor: int) -> bool:
    opened = os.fstat(descriptor)
    return (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
    ) == (
        marker.st_dev,
        marker.st_ino,
        marker.st_mode,
    )


def _markers_match(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
    )


def _checked_directory(path: Path, message: str) -> os.stat_result:
    marker = path.lstat()
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
        raise WeiboError(message, kind=WeiboErrorKind.API)
    return marker


@contextmanager
def _archive_lock(root: Path) -> Iterator[None]:
    physical = _physical_root(root)
    parent_marker = physical.parent.stat()
    lock_path = _archive_lock_path(physical)
    try:
        marker = lock_path.lstat()
    except FileNotFoundError:
        marker = None
    if marker is not None and stat.S_ISLNK(marker.st_mode):
        raise WeiboError("备份锁文件不能是符号链接", kind=WeiboErrorKind.API)
    flags = os.O_RDWR | os.O_CREAT | _O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise WeiboError("无法安全打开备份锁文件", original=exc) from exc
    locked = False
    try:
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        current_parent = physical.parent.resolve(strict=True).stat()
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (parent_marker.st_dev, parent_marker.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
        ):
            raise WeiboError("备份锁文件在打开时已变化", kind=WeiboErrorKind.API)
        try:
            if os.name == "nt":
                import msvcrt

                if opened.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise WeiboError("微博书正在备份，请稍后再试", kind=WeiboErrorKind.API) from exc
            raise WeiboError("获取微博书备份锁失败", original=exc) from exc
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


@dataclass(frozen=True)
class SyncResult:
    mode: SyncMode
    new_posts: int
    refreshed_posts: int
    changed_posts: int
    unavailable_posts: int
    generated_files: list[str]
    total_posts: int = 0


class PersonalArchiveSource(Protocol):
    def iter_profile_pages(
        self,
        uid: str,
        *,
        start_page: int = 1,
        pin_orders: dict[str, int] | None = None,
        next_pin_order: int = 1,
    ) -> Iterator[ProfilePage]: ...

    def fetch_post(self, uid: str, bid: str) -> Post: ...

    def fetch_recent_comments(
        self, post_id: str, limit: int = 10
    ) -> list[Comment]: ...


class IdentityProvider(Protocol):
    def whoami(self) -> dict: ...


@dataclass(frozen=True)
class StagedMedia:
    record: MediaRecord
    staged_path: Path


class MediaStager(Protocol):
    def stage(
        self,
        post: Post,
        comments: list[Comment],
        work_root: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[StagedMedia]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _media_payload(media: PostMedia, position: int) -> dict[str, object]:
    return {
        "type": media.type.value,
        "url": media.url,
        "thumbnail": media.thumbnail,
        "local_path": media.local_path,
        "local_thumb": media.local_thumb,
        "width": media.width,
        "height": media.height,
        "duration": media.duration,
        "video_cover": media.video_cover,
        "position": position,
    }


def _link_payload(link: LinkCard | None) -> dict[str, object] | None:
    return asdict(link) if link is not None else None


def _retweeted_payload(post: Post | None) -> dict[str, object] | None:
    if post is None:
        return None
    return {
        "bid": post.bid,
        "uid": post.uid,
        "user_name": post.user_name,
        "user_avatar": post.user_avatar,
        "text": post.text,
        "created_at": post.created_at.isoformat() if post.created_at else "",
        "source": post.source,
        "ip_location": post.ip_location,
        "visibility": post.visibility,
        "reposts_count": post.reposts_count,
        "comments_count": post.comments_count,
        "likes_count": post.likes_count,
        "link_card": _link_payload(post.link_card),
        "media": [_media_payload(item, index) for index, item in enumerate(post.media)],
    }


def post_to_record(post: Post) -> PostRecord:
    return PostRecord(
        bid=post.bid,
        uid=post.uid,
        text=post.text,
        created_at=post.created_at.isoformat() if post.created_at else "",
        source=post.source,
        ip_location=post.ip_location,
        is_pinned=post.is_pinned,
        pin_order=post.pin_order,
        visibility=post.visibility,
        reposts_count=post.reposts_count,
        comments_count=post.comments_count,
        likes_count=post.likes_count,
        retweeted_payload=_retweeted_payload(post.retweeted),
        link_card_payload=_link_payload(post.link_card),
        media_signature=[
            _media_payload(media, position)
            for position, media in enumerate(post.media)
        ],
    )


def _comment_payload(comment: Comment) -> dict[str, object]:
    return {
        "id": comment.id,
        "text": comment.text,
        "user_name": comment.user_name,
        "user_id": comment.user_id,
        "user_avatar": comment.user_avatar,
        "created_at": comment.created_at,
        "like_counts": comment.like_counts,
        "is_blogger": comment.is_blogger,
        "reply_to": comment.reply_to,
        "source": comment.source,
        "image_url": comment.image_url,
        "local_image": comment.local_image,
        "parent_id": comment.parent_id,
        "reply_to_name": comment.reply_to_name,
    }


def comments_to_records(bid: str, comments: list[Comment]) -> list[CommentRecord]:
    captured_at = _utc_now()
    records: list[CommentRecord] = []

    def append_comment(comment: Comment, inherited_parent: str | None = None) -> None:
        parent_id = comment.parent_id or inherited_parent
        records.append(
            CommentRecord(
                id=comment.id,
                post_bid=bid,
                parent_id=parent_id,
                payload=_comment_payload(comment),
                captured_at=captured_at,
            )
        )
        for reply in comment.replies:
            append_comment(reply, comment.id)

    for comment in comments:
        append_comment(comment)
    return records


@dataclass
class _Counters:
    new_posts: int = 0
    refreshed_posts: int = 0
    changed_posts: int = 0
    unavailable_posts: int = 0

    @classmethod
    def from_checkpoint(cls, checkpoint: dict[str, object]) -> "_Counters":
        raw = checkpoint.get("counters")
        if not isinstance(raw, dict):
            return cls()
        fields = ("new_posts", "refreshed_posts", "changed_posts", "unavailable_posts")
        if not all(type(raw.get(field, 0)) is int for field in fields):
            raise WeiboError("同步恢复点计数损坏", kind=WeiboErrorKind.PARSE)
        return cls(**{field: raw.get(field, 0) for field in fields})

    def payload(self) -> dict[str, int]:
        return {
            "new_posts": self.new_posts,
            "refreshed_posts": self.refreshed_posts,
            "changed_posts": self.changed_posts,
            "unavailable_posts": self.unavailable_posts,
        }


class PersonalArchiveSync:
    def __init__(
        self,
        root: str | Path,
        source: PersonalArchiveSource,
        identity_provider: IdentityProvider,
        *,
        media_stager: MediaStager | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        task_id: str | None = None,
        sync_run_started: Callable[[str], None] | None = None,
        pacing_scheduler: AdaptiveRequestScheduler | None = None,
    ) -> None:
        self.root = Path(root)
        self.source = source
        self.identity_provider = identity_provider
        self.media_stager = media_stager
        self.cancel_requested = cancel_requested or (lambda: False)
        self.pause_requested = pause_requested or (lambda: False)
        self.progress_callback = progress_callback
        if task_id is not None and re.fullmatch(r"[0-9a-f]{12}", task_id) is None:
            raise WeiboError("持久任务标识无效", kind=WeiboErrorKind.API)
        self.task_id = task_id
        self.sync_run_started = sync_run_started
        self.pacing_scheduler = pacing_scheduler

    def _emit_progress(
        self,
        phase: str,
        pct: float,
        detail: str,
        *,
        current: int,
        total: int | None,
        unit: str,
    ) -> None:
        if self.progress_callback is not None:
            self.progress_callback({
                "phase": phase,
                "pct": pct,
                "detail": detail,
                "current": current,
                "total": total,
                "unit": unit,
            })

    def run(self, mode: SyncMode) -> SyncResult:
        try:
            self.root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WeiboError("无法建立微博书父目录", original=exc) from exc
        physical = _physical_root(self.root)
        with _archive_lock(physical):
            self.root = physical
            result = self._run_locked(mode)
        completed = result.new_posts + result.refreshed_posts
        self._emit_progress(
            "complete", 1.0, "微博书归档已完成",
            current=completed, total=completed, unit="post",
        )
        return result

    def _run_locked(self, mode: SyncMode) -> SyncResult:
        identity = self.identity_provider.whoami()
        try:
            self_uid = identity["uid"]
            screen_name = identity["screen_name"]
        except (KeyError, TypeError) as exc:
            raise WeiboError(
                "无法读取当前登录账号信息",
                kind=WeiboErrorKind.PARSE,
                original=exc,
            ) from exc
        if (
            not isinstance(self_uid, str)
            or not self_uid.strip()
            or not isinstance(screen_name, str)
            or not screen_name.strip()
        ):
            raise WeiboError(
                "无法读取当前登录账号信息：UID 和昵称必须是非空字符串",
                kind=WeiboErrorKind.PARSE,
            )
        self._emit_progress(
            "identify", 0.02, f"已识别 @{screen_name}",
            current=1, total=1, unit="account",
        )

        if mode not in ("create", "incremental", "rebuild"):
            raise WeiboError(f"不支持的同步模式：{mode}", kind=WeiboErrorKind.API)

        self._check_cancelled()
        if not self.root.name or self.root.name in {".", ".."}:
            raise WeiboError("微博书目录路径不安全", kind=WeiboErrorKind.API)
        try:
            self._recover_rebuild_journal(self_uid)
        except OSError as exc:
            raise WeiboError("恢复中断的微博书重建失败，恢复状态已保留", original=exc) from exc
        self._validate_target_identity(mode, self_uid)
        if mode == "incremental":
            return self._run_incremental(self_uid)
        return self._run_replacement(mode, self_uid, screen_name)

    def _validate_target_identity(self, mode: SyncMode, uid: str) -> None:
        if self.root.is_symlink():
            raise WeiboError("微博书目录不能是符号链接", kind=WeiboErrorKind.API)
        if mode in ("incremental", "rebuild"):
            self._assert_safe_archive_markers()
            repository = ArchiveRepository.open(self.root, uid)
            repository.close()

    def _assert_safe_archive_markers(self) -> None:
        markers = (
            (self.root, "directory"),
            (self.root / "manifest.json", "file"),
            (self.root / "data", "directory"),
            (self.root / "data" / "archive.db", "file"),
        )
        for path, expected in markers:
            try:
                value = path.lstat()
            except OSError as exc:
                raise WeiboError(
                    "微博书归档标记不完整",
                    kind=WeiboErrorKind.PARSE,
                    original=exc,
                ) from exc
            if stat.S_ISLNK(value.st_mode):
                raise WeiboError("微博书归档标记不能是符号链接", kind=WeiboErrorKind.API)
            valid = (
                stat.S_ISDIR(value.st_mode)
                if expected == "directory"
                else stat.S_ISREG(value.st_mode)
            )
            if not valid:
                raise WeiboError("微博书归档标记类型错误", kind=WeiboErrorKind.PARSE)

    def _run_replacement(
        self, mode: Literal["create", "rebuild"], uid: str, screen_name: str
    ) -> SyncResult:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if mode == "create" and self.root.exists():
            if not self.root.is_dir() or any(self.root.iterdir()):
                raise WeiboError("首次备份只能使用空目录", kind=WeiboErrorKind.API)

        temporary = (
            self.root.parent / f".{self.root.name}.{mode}-task-{self.task_id}"
            if self.task_id is not None
            else self.root.parent / f".{self.root.name}.{mode}-{uuid.uuid4().hex}"
        )
        repository: ArchiveRepository | None = None
        try:
            if temporary.exists() or temporary.is_symlink():
                if self.task_id is None:
                    raise WeiboError("微博书临时归档已存在", kind=WeiboErrorKind.API)
                marker = temporary.lstat()
                if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
                    raise WeiboError("任务临时归档路径不安全", kind=WeiboErrorKind.API)
                if temporary != _physical_named_path(temporary):
                    raise WeiboError("任务临时归档路径越界", kind=WeiboErrorKind.API)
                repository = ArchiveRepository.open(temporary, uid)
                resume = True
            else:
                repository = ArchiveRepository.create(temporary, uid, screen_name)
                resume = False
            result = self._execute(repository, mode, uid, resume=resume)
            repository.close()
            repository = None
            self._replace_formal_directory(temporary, mode, uid)
            return result
        except OperationPaused:
            raise
        except BaseException:
            if repository is not None:
                repository.close()
                repository = None
            # 意外中断（崩溃/断电/强杀）且任务可恢复（有 task_id）时保留
            # 临时归档：同步恢复点就在其中，删除会让「继续任务」永久失去
            # 已抓进度。清理只发生在用户主动放弃（_cleanup_local_state）
            # 或提交阶段状态机接管时。无 task_id 的临时归档无法续跑，
            # 维持原有即失败即清理语义。
            if self.task_id is None and not _rebuild_state_path(self.root).exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        finally:
            if repository is not None:
                repository.close()

    def _replace_formal_directory(
        self,
        temporary: Path,
        mode: Literal["create", "rebuild"],
        uid: str,
    ) -> None:
        if mode == "create":
            preserved_empty = self.root.exists()
            backup = self.root.parent / f".{self.root.name}.previous-{uuid.uuid4().hex}"
            state_path = _rebuild_state_path(self.root)
            state = self._rebuild_state(temporary, backup, "create_prepared", uid)
            _write_state_atomic(state_path, state)
            try:
                if preserved_empty:
                    self.root.rmdir()
                    _fsync_directory(self.root.parent)
                    state = self._rebuild_state(
                        temporary, backup, "create_empty_removed", uid
                    )
                    _write_state_atomic(state_path, state)
                os.replace(temporary, self.root)
                _fsync_directory(self.root.parent)
                state = self._rebuild_state(
                    temporary, backup, "create_promoted", uid
                )
                _write_state_atomic(state_path, state)
                validation = ArchiveRepository.open(self.root, uid)
                validation.close()
                state_path.unlink()
                _fsync_directory(self.root.parent)
            except OSError as exc:
                raise WeiboError("建立正式微博书目录失败，恢复状态已保留", original=exc) from exc
            return

        backup = self.root.parent / f".{self.root.name}.previous-{uuid.uuid4().hex}"
        state_path = _rebuild_state_path(self.root)
        state = self._rebuild_state(temporary, backup, "prepared", uid)
        _write_state_atomic(state_path, state)
        try:
            os.replace(self.root, backup)
            _fsync_directory(self.root.parent)
            state = self._rebuild_state(temporary, backup, "backup_moved", uid)
            _write_state_atomic(state_path, state)
            try:
                os.replace(temporary, self.root)
                _fsync_directory(self.root.parent)
                state = self._rebuild_state(temporary, backup, "temp_promoted", uid)
                _write_state_atomic(state_path, state)
            except OSError as replace_exc:
                try:
                    os.replace(backup, self.root)
                    _fsync_directory(self.root.parent)
                    shutil.rmtree(temporary, ignore_errors=True)
                    state_path.unlink()
                    _fsync_directory(self.root.parent)
                except OSError as rollback_exc:
                    raise WeiboError(
                        "替换微博书失败且回滚失败，恢复状态已保留",
                        original=rollback_exc,
                    ) from replace_exc
                raise
        except OSError as exc:
            raise WeiboError("替换正式微博书目录失败", original=exc) from exc
        validation = ArchiveRepository.open(self.root, uid)
        validation.close()
        state = self._rebuild_state(temporary, backup, "completed", uid)
        _write_state_atomic(state_path, state)
        try:
            shutil.rmtree(backup)
            _fsync_directory(self.root.parent)
            state_path.unlink()
            _fsync_directory(self.root.parent)
        except OSError as exc:
            raise WeiboError("完成微博书重建清理失败，恢复状态已保留", original=exc) from exc

    def _rebuild_state(
        self,
        temporary: Path,
        backup: Path,
        phase: str,
        uid: str,
    ) -> dict[str, str]:
        physical_root = _physical_root(self.root)
        physical_temporary = _physical_named_path(temporary)
        physical_backup = _physical_named_path(backup)
        if not (
            physical_root.parent == physical_temporary.parent
            == physical_backup.parent
        ):
            raise WeiboError("重建恢复路径不在同一物理父目录", kind=WeiboErrorKind.API)
        return {
            "root": str(physical_root),
            "temp": str(physical_temporary),
            "backup": str(physical_backup),
            "phase": phase,
            "uid": uid,
        }

    def _recover_rebuild_journal(self, uid: str) -> None:
        state_path = _rebuild_state_path(self.root)
        try:
            marker = state_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
            raise WeiboError("重建恢复状态文件不安全", kind=WeiboErrorKind.API)
        try:
            descriptor = os.open(
                state_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(descriptor)
                current = state_path.lstat()
                if (
                    stat.S_ISLNK(current.st_mode)
                    or (opened.st_dev, opened.st_ino, opened.st_size)
                    != (current.st_dev, current.st_ino, current.st_size)
                ):
                    raise WeiboError("重建恢复状态文件在读取时已变化", kind=WeiboErrorKind.API)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(descriptor)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WeiboError("重建恢复状态损坏", kind=WeiboErrorKind.PARSE, original=exc) from exc
        required = {"root", "temp", "backup", "phase", "uid"}
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or not all(isinstance(payload[field], str) for field in required)
            or payload["root"] != str(_physical_root(self.root))
            or payload["uid"] != uid
            or payload["phase"]
            not in {
                "prepared", "backup_moved", "temp_promoted", "completed",
                "create_prepared", "create_empty_removed", "create_promoted",
            }
        ):
            raise WeiboError("重建恢复状态字段损坏", kind=WeiboErrorKind.PARSE)
        temporary = Path(payload["temp"])
        backup = Path(payload["backup"])
        parent = self.root.parent.resolve(strict=True)
        create_phase = payload["phase"].startswith("create_")
        expected_temp_prefix = (
            f".{self.root.name}.create-"
            if create_phase
            else f".{self.root.name}.rebuild-"
        )
        if (
            temporary != _physical_named_path(temporary)
            or backup != _physical_named_path(backup)
            or temporary.parent != parent
            or backup.parent != parent
            or len({self.root.name, temporary.name, backup.name}) != 3
            or not temporary.name.startswith(expected_temp_prefix)
            or not backup.name.startswith(f".{self.root.name}.previous-")
            or temporary.is_symlink()
            or backup.is_symlink()
        ):
            raise WeiboError("重建恢复路径不安全", kind=WeiboErrorKind.API)

        phase = payload["phase"]
        if create_phase:
            self._recover_create_journal(temporary, state_path, phase, uid)
            return
        if not self.root.exists():
            if backup.is_dir():
                os.replace(backup, self.root)
                _fsync_directory(self.root.parent)
                self._remove_rebuild_artifacts(temporary, state_path)
                return
            raise WeiboError("正式微博书缺失且无可用备份，恢复状态已保留")

        if phase == "prepared":
            self._remove_rebuild_artifacts(temporary, state_path)
            return

        new_is_valid = False
        try:
            validation = ArchiveRepository.open(self.root, uid)
            validation.close()
            new_is_valid = True
        except WeiboError:
            new_is_valid = False
        if new_is_valid and phase in {"backup_moved", "temp_promoted", "completed"}:
            if backup.is_dir():
                shutil.rmtree(backup)
            self._remove_rebuild_artifacts(temporary, state_path)
            return
        if backup.is_dir():
            failed = self.root.parent / f".{self.root.name}.failed-{uuid.uuid4().hex}"
            os.replace(self.root, failed)
            os.replace(backup, self.root)
            _fsync_directory(self.root.parent)
            shutil.rmtree(failed, ignore_errors=True)
            self._remove_rebuild_artifacts(temporary, state_path)
            return
        raise WeiboError("新微博书校验失败且无可用备份，恢复状态已保留")

    def _recover_create_journal(
        self,
        temporary: Path,
        state_path: Path,
        phase: str,
        uid: str,
    ) -> None:
        if self.root.exists():
            if phase == "create_prepared" and self.root.is_dir() and not any(self.root.iterdir()):
                self._remove_rebuild_artifacts(temporary, state_path)
                return
            try:
                validation = ArchiveRepository.open(self.root, uid)
                validation.close()
            except WeiboError as exc:
                raise WeiboError("首次备份恢复时正式目录已变化，恢复状态已保留", original=exc) from exc
            self._remove_rebuild_artifacts(temporary, state_path)
            return
        try:
            validation = ArchiveRepository.open(temporary, uid)
            validation.close()
        except WeiboError:
            self.root.mkdir()
            _fsync_directory(self.root.parent)
            self._remove_rebuild_artifacts(temporary, state_path)
            return
        os.replace(temporary, self.root)
        _fsync_directory(self.root.parent)
        validation = ArchiveRepository.open(self.root, uid)
        validation.close()
        self._remove_rebuild_artifacts(temporary, state_path)

    def _remove_rebuild_artifacts(
        self,
        temporary: Path,
        state_path: Path,
    ) -> None:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        state_path.unlink()
        _fsync_directory(self.root.parent)

    def _run_incremental(self, uid: str) -> SyncResult:
        repository = ArchiveRepository.open(self.root, uid)
        try:
            reconciled = self._reconcile_committing(repository)
            if reconciled is not None:
                return reconciled
            return self._execute(repository, "incremental", uid, resume=True)
        finally:
            repository.close()

    def _reconcile_committing(
        self, repository: ArchiveRepository
    ) -> SyncResult | None:
        pending = repository.get_latest_committing_sync()
        if pending is None:
            return None
        summary = pending.summary
        if summary is None:
            raise WeiboError("同步提交摘要损坏", kind=WeiboErrorKind.PARSE)
        successful_at = pending.checkpoint.get("successful_at")
        generated_files = summary.get("generated_files")
        counter_names = (
            "new_posts",
            "refreshed_posts",
            "changed_posts",
            "unavailable_posts",
        )
        if (
            not isinstance(successful_at, str)
            or not successful_at
            or not isinstance(generated_files, list)
            or not all(isinstance(item, str) for item in generated_files)
            or any(
                not isinstance(summary.get(name), int)
                or isinstance(summary.get(name), bool)
                or int(summary[name]) < 0
                for name in counter_names
            )
        ):
            raise WeiboError("同步提交状态损坏", kind=WeiboErrorKind.PARSE)
        repository.update_manifest_success(successful_at)
        repository.finish_sync(pending.run_id, "done", summary)
        if pending.checkpoint.get("promotion") is None:
            self._cleanup_empty_run_work(repository._root, pending.run_id)
        return SyncResult(
            pending.mode,
            generated_files=list(generated_files),
            **{name: int(summary[name]) for name in counter_names},
        )

    @staticmethod
    def _cleanup_empty_run_work(root: Path, run_id: str) -> None:
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError("run_id 不是规范 UUID")
        except ValueError as exc:
            raise WeiboError("同步提交 run_id 损坏", kind=WeiboErrorKind.PARSE, original=exc) from exc
        if not _SUPPORTS_DIRECTORY_FDS:
            root_marker = _checked_directory(root, "归档目录在清理时已变化")
            work_path = root / ".work"
            try:
                work_marker = _checked_directory(
                    work_path, "同步暂存目录不安全，已停止自动清理"
                )
            except FileNotFoundError:
                return
            run_path = work_path / run_id
            try:
                run_marker = _checked_directory(
                    run_path, "同步运行暂存目录不安全，已停止自动清理"
                )
            except FileNotFoundError:
                return
            if any(run_path.iterdir()):
                return
            if not (
                _markers_match(root_marker, _checked_directory(root, "归档目录在清理时已变化"))
                and _markers_match(work_marker, _checked_directory(work_path, "同步暂存目录在清理时已变化"))
                and _markers_match(run_marker, _checked_directory(run_path, "同步运行暂存目录在清理时已变化"))
            ):
                raise WeiboError("同步运行暂存目录在清理时已变化", kind=WeiboErrorKind.API)
            run_path.rmdir()
            _fsync_directory(work_path)
            return
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        root_fd = os.open(root, flags)
        work_fd: int | None = None
        run_fd: int | None = None
        try:
            try:
                work_marker = os.stat(".work", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(work_marker.st_mode) or not stat.S_ISDIR(work_marker.st_mode):
                raise WeiboError("同步暂存目录不安全，已停止自动清理", kind=WeiboErrorKind.API)
            work_fd = os.open(".work", flags, dir_fd=root_fd)
            if not _opened_matches(work_marker, work_fd):
                raise WeiboError("同步暂存目录在清理时已变化", kind=WeiboErrorKind.API)
            try:
                run_marker = os.stat(run_id, dir_fd=work_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(run_marker.st_mode) or not stat.S_ISDIR(run_marker.st_mode):
                raise WeiboError("同步运行暂存目录不安全，已停止自动清理", kind=WeiboErrorKind.API)
            run_fd = os.open(run_id, flags, dir_fd=work_fd)
            if not _opened_matches(run_marker, run_fd):
                raise WeiboError("同步运行暂存目录在清理时已变化", kind=WeiboErrorKind.API)
            if os.listdir(run_fd):
                return
            os.close(run_fd)
            run_fd = None
            os.rmdir(run_id, dir_fd=work_fd)
            os.fsync(work_fd)
        finally:
            if run_fd is not None:
                os.close(run_fd)
            if work_fd is not None:
                os.close(work_fd)
            os.close(root_fd)

    def _execute(
        self,
        repository: ArchiveRepository,
        mode: SyncMode,
        uid: str,
        *,
        resume: bool,
    ) -> SyncResult:
        previous = repository.get_unfinished_sync(mode) if resume else None
        checkpoint = dict(previous.checkpoint) if previous is not None else {}
        if previous is not None and checkpoint.get("promotion") is not None:
            self._recover_promotion(
                repository,
                previous.run_id,
                checkpoint,
            )
        completed_raw = checkpoint.get("completed_bids", [])
        if not isinstance(completed_raw, list) or not all(
            isinstance(item, str) and bool(item) for item in completed_raw
        ):
            raise WeiboError("同步恢复点已损坏", kind=WeiboErrorKind.PARSE)
        if len(set(completed_raw)) != len(completed_raw):
            raise WeiboError("同步恢复点已损坏：已完成 BID 重复", kind=WeiboErrorKind.PARSE)
        completed = set(completed_raw)
        counters = _Counters.from_checkpoint(checkpoint)
        checkpoint["resumed"] = previous is not None
        checkpoint["completed_bids"] = list(completed_raw)
        checkpoint["counters"] = counters.payload()
        checkpoint.setdefault("pages_completed", 0)

        run_id = repository.begin_sync(mode)
        if self.sync_run_started is not None:
            self.sync_run_started(run_id)
        work_root = repository._root / ".work" / run_id
        generated_files: list[str] = []
        committing = False
        try:
            repository.update_sync_checkpoint(run_id, checkpoint)
            self._prepare_work_root(repository._root, run_id)
            self._check_cancelled()
            saved_plan = self._saved_plan(checkpoint) if previous is not None else None
            if saved_plan is None:
                pending, new_bids, refresh_bids, profile_metadata = self._discover(
                    repository, run_id, checkpoint, mode, uid
                )
                checkpoint["pending_bids"] = pending
                checkpoint["new_bids"] = new_bids
                checkpoint["refresh_bids"] = refresh_bids
                checkpoint["profile_metadata"] = profile_metadata
                repository.update_sync_checkpoint(run_id, checkpoint)
            else:
                pending, new_bids, refresh_bids, profile_metadata = saved_plan
            if self.pacing_scheduler is not None:
                self.pacing_scheduler.set_known_remaining(
                    posts=sum(1 for bid in pending if bid not in completed)
                )
            known = set(refresh_bids)
            total_pending = len(pending)
            self._emit_progress(
                "discover", 0.15, f"已确认 {total_pending} 条待处理微博",
                current=0, total=total_pending, unit="post",
            )
            if total_pending == 0:
                for phase, pct, detail in (
                    ("extract", 0.35, "没有微博需要提取"),
                    ("comments", 0.55, "没有评论需要处理"),
                    ("media", 0.75, "没有媒体需要处理"),
                ):
                    self._emit_progress(
                        phase, pct, detail, current=0, total=0, unit="post"
                    )
            for current, bid in enumerate(pending, 1):
                if bid in completed:
                    continue
                self._check_cancelled()
                counters = self._process_bid(
                    repository,
                    work_root,
                    run_id,
                    checkpoint,
                    pending,
                    completed,
                    uid,
                    bid,
                    bid in known,
                    counters,
                    current,
                    total_pending,
                    profile_metadata[bid],
                )
                completed.add(bid)

            self._check_cancelled()
            self._emit_progress(
                "generate", 0.90, "正在提交归档索引",
                current=0, total=1, unit="archive",
            )
            successful_at = _utc_now()
            summary = {
                **counters.payload(),
                "generated_files": generated_files,
                "resumed": previous is not None,
            }
            checkpoint["successful_at"] = successful_at
            checkpoint["completion_summary"] = summary
            repository.mark_sync_committing(run_id, checkpoint, summary)
            committing = True
            repository.update_manifest_success(successful_at)
            repository.finish_sync(run_id, "done", summary)
            self._emit_progress(
                "generate", 0.95, "归档索引已提交",
                current=1, total=1, unit="archive",
            )
            return SyncResult(mode, generated_files=generated_files, **counters.payload())
        except OperationCancelled:
            repository.clear_sync_checkpoint(run_id, "cancelled")
            raise
        except OperationPaused:
            repository.finish_sync(run_id, "paused", {**counters.payload(), "message": "任务已暂停"})
            raise
        except Exception as exc:
            if not committing:
                try:
                    repository.finish_sync(run_id, "error", {**counters.payload(), "error": str(exc)})
                except WeiboError:
                    pass
            if isinstance(exc, WeiboError):
                raise
            converted = WeiboError(
                f"微博书同步失败：{exc}",
                kind=classify_error(exc),
                original=exc,
            )
            raise converted from exc
        finally:
            if checkpoint.get("promotion") is None:
                shutil.rmtree(work_root, ignore_errors=True)
                try:
                    work_root.parent.rmdir()
                except OSError:
                    pass

    def _discover(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        mode: SyncMode,
        uid: str,
    ) -> tuple[list[str], list[str], list[str], dict[str, dict[str, object]]]:
        if mode in {"create", "rebuild"}:
            return self._discover_replacement_pages(
                repository,
                run_id,
                checkpoint,
                uid,
            )
        pages_completed = int(checkpoint.get("pages_completed", 0))

        def pages() -> Iterator[ProfilePage]:
            nonlocal pages_completed
            for value in self.source.iter_profile_pages(uid):
                self._check_cancelled()
                pages_completed += 1
                checkpoint["pages_completed"] = pages_completed
                repository.update_sync_checkpoint(run_id, checkpoint)
                self._emit_progress(
                    "discover", 0.08, f"已发现 {pages_completed} 页",
                    current=pages_completed, total=None, unit="page",
                )
                yield value

        known = repository.list_known_bids()
        if mode == "incremental":
            discovered = discover_incremental(
                pages(), known, refresh_limit=_INCREMENTAL_REFRESH_LIMIT
            )
            pending = discovered.new_bids + discovered.refresh_bids
            refresh_bids = list(discovered.refresh_bids)
            current_pinned = {
                bid for bid, item in discovered.profile_metadata.items()
                if item.is_pinned
            }
            stale_pinned = [
                bid for bid in repository.list_pinned_bids()
                if bid not in current_pinned
            ]
            for bid in stale_pinned:
                if bid not in pending:
                    pending.append(bid)
                    refresh_bids.append(bid)
                discovered.profile_metadata[bid] = ProfileItem(bid, False, None)
            return (
                pending,
                discovered.new_bids,
                refresh_bids,
                {
                    bid: {
                        "is_pinned": discovered.profile_metadata[bid].is_pinned,
                        "pin_order": discovered.profile_metadata[bid].pin_order,
                    }
                    for bid in pending
                },
            )

        raise WeiboError("同步模式无法执行主页发现", kind=WeiboErrorKind.API)

    def _discover_replacement_pages(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        uid: str,
    ) -> tuple[list[str], list[str], list[str], dict[str, dict[str, object]]]:
        discovered = checkpoint.get("discovered_bids", [])
        metadata = checkpoint.get("discovered_profile_metadata", {})
        start_page = checkpoint.get("next_profile_page", 1)
        next_pin_order = checkpoint.get("next_pin_order", 1)
        if (
            not isinstance(discovered, list)
            or not all(isinstance(item, str) and item for item in discovered)
            or len(set(discovered)) != len(discovered)
            or not isinstance(metadata, dict)
            or set(metadata) != set(discovered)
            or type(start_page) is not int
            or start_page < 1
            or type(next_pin_order) is not int
            or next_pin_order < 1
        ):
            raise WeiboError("主页发现恢复点已损坏", kind=WeiboErrorKind.PARSE)
        normalized_metadata: dict[str, dict[str, object]] = {}
        pin_orders: dict[str, int] = {}
        for bid in discovered:
            value = metadata[bid]
            if not isinstance(value, dict) or set(value) != {"is_pinned", "pin_order"}:
                raise WeiboError("主页发现恢复点已损坏", kind=WeiboErrorKind.PARSE)
            is_pinned = value["is_pinned"]
            pin_order = value["pin_order"]
            if type(is_pinned) is not bool or not (
                (is_pinned and type(pin_order) is int and pin_order > 0)
                or (not is_pinned and pin_order is None)
            ):
                raise WeiboError("主页发现恢复点已损坏", kind=WeiboErrorKind.PARSE)
            normalized_metadata[bid] = {
                "is_pinned": is_pinned,
                "pin_order": pin_order,
            }
            if is_pinned:
                pin_orders[bid] = pin_order

        if start_page == 1:
            pages = self.source.iter_profile_pages(uid)
        else:
            pages = self.source.iter_profile_pages(
                uid,
                start_page=start_page,
                pin_orders=pin_orders,
                next_pin_order=next_pin_order,
            )
        seen = set(discovered)
        page_number = start_page
        for profile_page in pages:
            for item in profile_page.items:
                if item.bid in seen:
                    continue
                seen.add(item.bid)
                discovered.append(item.bid)
                normalized_metadata[item.bid] = {
                    "is_pinned": item.is_pinned,
                    "pin_order": item.pin_order,
                }
                if item.is_pinned and item.pin_order is not None:
                    pin_orders[item.bid] = item.pin_order
                    next_pin_order = max(next_pin_order, item.pin_order + 1)
            page_number += 1
            checkpoint["pages_completed"] = page_number - 1
            checkpoint["discovered_bids"] = list(discovered)
            checkpoint["discovered_profile_metadata"] = dict(normalized_metadata)
            checkpoint["next_profile_page"] = page_number
            checkpoint["next_pin_order"] = next_pin_order
            repository.update_sync_checkpoint(run_id, checkpoint)
            self._emit_progress(
                "discover",
                0.08,
                f"已发现 {page_number - 1} 页",
                current=page_number - 1,
                total=None,
                unit="page",
            )
            self._check_cancelled()
            if profile_page.is_last:
                break
        return discovered, list(discovered), [], normalized_metadata

    def _saved_plan(
        self, checkpoint: dict[str, object]
    ) -> tuple[list[str], list[str], list[str], dict[str, dict[str, object]]] | None:
        if "pending_bids" not in checkpoint:
            if checkpoint.get("completed_bids"):
                raise WeiboError(
                    "同步恢复点已损坏：缺少待处理 BID",
                    kind=WeiboErrorKind.PARSE,
                )
            return None

        def require_bid_list(field: str) -> list[str]:
            value = checkpoint.get(field)
            if (
                not isinstance(value, list)
                or not all(isinstance(item, str) and bool(item) for item in value)
                or len(set(value)) != len(value)
            ):
                raise WeiboError(
                    f"同步恢复点已损坏：{field} 无效",
                    kind=WeiboErrorKind.PARSE,
                )
            return value

        pending = require_bid_list("pending_bids")
        new_bids = require_bid_list("new_bids")
        refresh_bids = require_bid_list("refresh_bids")
        if new_bids + refresh_bids != pending:
            raise WeiboError(
                "同步恢复点已损坏：BID 分类与待处理列表不一致",
                kind=WeiboErrorKind.PARSE,
            )
        raw_metadata = checkpoint.get("profile_metadata")
        if raw_metadata is None and not pending:
            raw_metadata = {}
        if not isinstance(raw_metadata, dict) or set(raw_metadata) != set(pending):
            raise WeiboError("同步恢复点损坏：主页元数据无效", kind=WeiboErrorKind.PARSE)
        profile_metadata: dict[str, dict[str, object]] = {}
        for bid, value in raw_metadata.items():
            if not isinstance(value, dict) or set(value) != {"is_pinned", "pin_order"}:
                raise WeiboError("同步恢复点损坏：主页元数据无效", kind=WeiboErrorKind.PARSE)
            is_pinned = value["is_pinned"]
            pin_order = value["pin_order"]
            if type(is_pinned) is not bool or not (
                (is_pinned and type(pin_order) is int and pin_order > 0)
                or (not is_pinned and pin_order is None)
            ):
                raise WeiboError("同步恢复点损坏：主页元数据无效", kind=WeiboErrorKind.PARSE)
            profile_metadata[bid] = {"is_pinned": is_pinned, "pin_order": pin_order}
        completed = checkpoint.get("completed_bids", [])
        if not set(completed).issubset(set(pending)):
            raise WeiboError(
                "同步恢复点已损坏：已完成 BID 不在待处理列表中",
                kind=WeiboErrorKind.PARSE,
            )
        return pending, new_bids, refresh_bids, profile_metadata

    def _prepare_work_root(self, root: Path, run_id: str) -> None:
        if not _SUPPORTS_DIRECTORY_FDS:
            try:
                root_marker = _checked_directory(root, "归档目录在打开时已变化")
                work_path = root / ".work"
                try:
                    work_path.mkdir()
                except FileExistsError:
                    pass
                work_marker = _checked_directory(work_path, ".work 必须是目录")
                run_path = work_path / run_id
                run_path.mkdir()
                run_marker = _checked_directory(run_path, "媒体暂存目录在打开时已变化")
                if not (
                    _markers_match(root_marker, _checked_directory(root, "归档目录在打开时已变化"))
                    and _markers_match(work_marker, _checked_directory(work_path, ".work 在打开时已变化"))
                    and _markers_match(run_marker, _checked_directory(run_path, "媒体暂存目录在打开时已变化"))
                ):
                    raise WeiboError("媒体暂存目录在打开时已变化", kind=WeiboErrorKind.API)
            except WeiboError:
                raise
            except OSError as exc:
                raise WeiboError("建立安全媒体暂存目录失败", original=exc) from exc
            try:
                run_path.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise WeiboError("媒体暂存目录越界", kind=WeiboErrorKind.API, original=exc) from exc
            return
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        root_fd = os.open(root, flags)
        try:
            if not _opened_matches(root.lstat(), root_fd):
                raise WeiboError("归档目录在打开时已变化", kind=WeiboErrorKind.API)
            try:
                marker = os.stat(".work", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(".work", dir_fd=root_fd)
                marker = os.stat(".work", dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(marker.st_mode):
                raise WeiboError(".work 不能是符号链接", kind=WeiboErrorKind.API)
            if not stat.S_ISDIR(marker.st_mode):
                raise WeiboError(".work 必须是目录", kind=WeiboErrorKind.API)
            work_fd = os.open(".work", flags, dir_fd=root_fd)
            try:
                if not _opened_matches(marker, work_fd):
                    raise WeiboError(".work 在打开时已变化", kind=WeiboErrorKind.API)
                os.mkdir(run_id, dir_fd=work_fd)
                run_marker = os.stat(
                    run_id,
                    dir_fd=work_fd,
                    follow_symlinks=False,
                )
                run_fd = os.open(run_id, flags, dir_fd=work_fd)
                if not _opened_matches(run_marker, run_fd):
                    os.close(run_fd)
                    raise WeiboError("媒体暂存目录在打开时已变化", kind=WeiboErrorKind.API)
                os.close(run_fd)
            finally:
                os.close(work_fd)
        except OSError as exc:
            raise WeiboError("建立安全媒体暂存目录失败", original=exc) from exc
        finally:
            os.close(root_fd)
        try:
            (root / ".work" / run_id).resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise WeiboError("媒体暂存目录越界", kind=WeiboErrorKind.API, original=exc) from exc

    def _process_bid(
        self,
        repository: ArchiveRepository,
        work_root: Path,
        run_id: str,
        checkpoint: dict[str, object],
        pending: list[str],
        completed: set[str],
        uid: str,
        bid: str,
        known: bool,
        counters: _Counters,
        current: int,
        total: int,
        profile_metadata: dict[str, object],
    ) -> _Counters:
        base = 0.20
        span = 0.65
        denominator = max(1, total * 3)

        def phase_pct(offset: int) -> float:
            return base + span * (((current - 1) * 3 + offset) / denominator)

        try:
            post = self.source.fetch_post(uid, bid)
        except WeiboError as exc:
            if exc.kind is not WeiboErrorKind.NOT_FOUND:
                raise
            self._emit_progress(
                "extract", phase_pct(1), f"已检查 {current}/{total} 条微博",
                current=current, total=total, unit="post",
            )
            self._emit_progress(
                "comments", phase_pct(2), f"跳过不可见微博的评论 {current}/{total}",
                current=current, total=total, unit="post",
            )
            self._emit_progress(
                "media", phase_pct(3), f"跳过不可见微博的媒体 {current}/{total}",
                current=current, total=total, unit="post",
            )
            existing_post = repository.get_post(bid)
            next_counters = replace(
                counters,
                refreshed_posts=counters.refreshed_posts
                + (1 if known and existing_post is not None else 0),
                unavailable_posts=counters.unavailable_posts + 1,
            )
            with repository.transaction():
                if existing_post is not None:
                    result = repository.apply_post_change(
                        replace(
                            existing_post,
                            visibility="unavailable",
                            is_pinned=False,
                            pin_order=None,
                        )
                    )
                    if result.kind != "unchanged":
                        next_counters = replace(
                            next_counters,
                            changed_posts=next_counters.changed_posts + 1,
                        )
                next_checkpoint = self._completed_checkpoint(
                    checkpoint, pending, completed | {bid}, next_counters
                )
                repository.update_sync_checkpoint(run_id, next_checkpoint)
            checkpoint.clear()
            checkpoint.update(next_checkpoint)
            return next_counters

        self._emit_progress(
            "extract", phase_pct(1), f"已提取 {current}/{total} 条微博",
            current=current, total=total, unit="post",
        )

        self._check_cancelled()

        if post.bid != bid or post.uid != uid:
            # 主页时间轴可能混入广告/推广卡，详情接口返回的不是目标账号的帖子。
            # 跳过并记入恢复点，不中断整轮备份；已有档案数据保持不变（只增不减）。
            logger.warning(
                "微博详情与归档目标不一致，已跳过: 期望 bid=%s uid=%s, 实际 bid=%s uid=%s",
                bid, uid, post.bid, post.uid,
            )
            self._emit_progress(
                "extract", phase_pct(1), f"已提取 {current}/{total} 条微博",
                current=current, total=total, unit="post",
            )
            next_counters = replace(
                counters,
                unavailable_posts=counters.unavailable_posts + 1,
            )
            with repository.transaction():
                next_checkpoint = self._completed_checkpoint(
                    checkpoint, pending, completed | {bid}, next_counters
                )
                repository.update_sync_checkpoint(run_id, next_checkpoint)
            checkpoint.clear()
            checkpoint.update(next_checkpoint)
            return next_counters
        post.is_pinned = bool(profile_metadata["is_pinned"])
        pin_order = profile_metadata["pin_order"]
        post.pin_order = pin_order if isinstance(pin_order, int) else None

        comments = self.source.fetch_recent_comments(
            post.raw_bid or post.bid, limit=10
        )[:10]
        self._emit_progress(
            "comments", phase_pct(2), f"已处理 {current}/{total} 条微博的评论",
            current=current, total=total, unit="post",
        )
        self._check_cancelled()
        staged = (
            self.media_stager.stage(
                post,
                comments,
                work_root,
                cancel_requested=self.cancel_requested,
            )
            if self.media_stager is not None
            else []
        )
        self._emit_progress(
            "media", phase_pct(3), f"已处理 {current}/{total} 条微博的媒体",
            current=current, total=total, unit="post",
        )
        self._check_cancelled()
        promotion: dict[str, object] | None = None
        promotion_persisted = False
        promotion_applied = False
        database_committed = False
        next_counters = counters
        try:
            if staged:
                promotion = self._prepare_promotion(
                    repository._root,
                    work_root,
                    run_id,
                    staged,
                )
                prepared_checkpoint = dict(checkpoint)
                prepared_checkpoint["promotion"] = promotion
                repository.update_sync_checkpoint(run_id, prepared_checkpoint)
                checkpoint.clear()
                checkpoint.update(prepared_checkpoint)
                promotion_persisted = True
                self._check_cancelled()
                self._apply_promotion(
                    repository, run_id, checkpoint, promotion
                )
                promotion_applied = True
                promotion = {**promotion, "phase": "promoted"}
                promoted_checkpoint = dict(checkpoint)
                promoted_checkpoint["promotion"] = promotion
                repository.update_sync_checkpoint(run_id, promoted_checkpoint)
                checkpoint.clear()
                checkpoint.update(promoted_checkpoint)
            self._check_cancelled()
            with repository.transaction():
                change = repository.apply_post_change(post_to_record(post))
                repository.replace_current_comments(
                    bid, comments_to_records(bid, comments)
                )
                for item in staged:
                    repository.upsert_media(item.record)
                if change.kind == "new":
                    next_counters = replace(
                        counters, new_posts=counters.new_posts + 1
                    )
                elif known:
                    next_counters = replace(
                        counters,
                        refreshed_posts=counters.refreshed_posts + 1,
                        changed_posts=counters.changed_posts
                        + (1 if change.kind != "unchanged" else 0),
                    )
                next_checkpoint = self._completed_checkpoint(
                    checkpoint, pending, completed | {bid}, next_counters
                )
                if promotion is not None:
                    promotion = {**promotion, "phase": "db_committed"}
                    next_checkpoint["promotion"] = promotion
                repository.update_sync_checkpoint(run_id, next_checkpoint)
            database_committed = True
        except Exception:
            if promotion is not None:
                if database_committed:
                    self._finish_promotion(
                        repository,
                        run_id,
                        checkpoint,
                        promotion,
                    )
                elif promotion_applied or promotion_persisted:
                    self._restore_promotion(repository._root, run_id, promotion)
                    cleared = dict(checkpoint)
                    cleared["promotion"] = None
                    repository.update_sync_checkpoint(run_id, cleared)
                    checkpoint.clear()
                    checkpoint.update(cleared)
                else:
                    self._discard_promotion_work(repository._root, run_id, promotion)
            raise
        checkpoint.clear()
        checkpoint.update(next_checkpoint)
        if promotion is not None:
            self._finish_promotion(
                repository,
                run_id,
                checkpoint,
                promotion,
            )
        return next_counters

    @staticmethod
    def _completed_checkpoint(
        checkpoint: dict[str, object],
        pending: list[str],
        completed: set[str],
        counters: _Counters,
    ) -> dict[str, object]:
        updated = dict(checkpoint)
        updated["completed_bids"] = [
            item for item in pending if item in completed
        ]
        updated["counters"] = counters.payload()
        return updated

    def _prepare_promotion(
        self,
        root: Path,
        work_root: Path,
        run_id: str,
        staged: list[StagedMedia],
    ) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for index, item in enumerate(staged):
            local_path = item.record.local_path
            self._validate_media_target(local_path)
            safe_path = self._copy_staged_to_safe(
                work_root,
                Path(item.staged_path),
                index,
            )
            install_proof = self._install_proof(safe_path)
            expected_target: dict[str, object] = {"state": "missing"}
            if not _SUPPORTS_DIRECTORY_FDS:
                target_path = self._checked_media_target_path(root, local_path)
                try:
                    marker = target_path.lstat()
                except FileNotFoundError:
                    marker = None
                if marker is not None:
                    if stat.S_ISLNK(marker.st_mode):
                        raise WeiboError(
                            "媒体目标不能是符号链接",
                            kind=WeiboErrorKind.API,
                        )
                    if not stat.S_ISREG(marker.st_mode):
                        raise WeiboError(
                            "媒体目标必须是普通文件",
                            kind=WeiboErrorKind.API,
                        )
                    expected_target = self._target_state(marker)
            else:
                with self._open_media_parent(root, local_path) as (
                    parent_fd,
                    target_name,
                ):
                    try:
                        marker = os.stat(
                            target_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        marker = None
                    if marker is not None:
                        if stat.S_ISLNK(marker.st_mode):
                            raise WeiboError(
                                "媒体目标不能是符号链接",
                                kind=WeiboErrorKind.API,
                            )
                        if not stat.S_ISREG(marker.st_mode):
                            raise WeiboError(
                                "媒体目标必须是普通文件",
                                kind=WeiboErrorKind.API,
                            )
                        expected_target = self._target_state(marker)
            entries.append(
                {
                    "staged": safe_path.relative_to(root).as_posix(),
                    "target": local_path,
                    "backup": (
                        work_root / f"promotion-backup-{index}"
                    ).relative_to(root).as_posix(),
                    "expected_target": expected_target,
                    "installed_target": None,
                    "install_proof": install_proof,
                    "step": "prepared",
                }
            )
        promotion = {
            "run_id": run_id,
            "phase": "prepared",
            "entries": entries,
        }
        self._promotion_entries(promotion, run_id)
        return promotion

    @staticmethod
    def _install_proof(path: Path) -> dict[str, object]:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise WeiboError("媒体安装源必须是单链接普通文件", kind=WeiboErrorKind.API)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            identity = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in identity):
                raise WeiboError("媒体安装源在计算内容证明时已变化", kind=WeiboErrorKind.API)
            return {
                "sha256": digest.hexdigest(),
                "size": before.st_size,
                "staged_dev": before.st_dev if os.name != "nt" else None,
                "staged_ino": before.st_ino if os.name != "nt" else None,
            }
        except OSError as exc:
            raise WeiboError("无法计算媒体安装内容证明", original=exc) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _copy_staged_to_safe(
        self,
        work_root: Path,
        source: Path,
        index: int,
    ) -> Path:
        try:
            relative = source.resolve(strict=True).relative_to(
                work_root.resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise WeiboError("媒体暂存路径不安全", kind=WeiboErrorKind.API, original=exc) from exc
        if not _SUPPORTS_DIRECTORY_FDS:
            marker = source.lstat()
            root_marker = _checked_directory(work_root, "媒体暂存目录在打开时已变化")
            if (
                stat.S_ISLNK(marker.st_mode)
                or not stat.S_ISREG(marker.st_mode)
            ):
                raise WeiboError("媒体暂存文件类型不安全", kind=WeiboErrorKind.API)
            if marker.st_nlink != 1:
                raise WeiboError("媒体暂存文件不能是硬链接", kind=WeiboErrorKind.API)
            safe_path = work_root / f"promotion-safe-{index}-{uuid.uuid4().hex}"
            source_fd = os.open(source, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
            safe_fd: int | None = None
            try:
                opened = os.fstat(source_fd)
                current = source.lstat()
                if (
                    (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_nlink)
                    != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_nlink)
                    or opened.st_nlink != 1
                ):
                    raise WeiboError("媒体暂存文件在打开时已变化", kind=WeiboErrorKind.API)
                safe_fd = os.open(
                    safe_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _O_NOFOLLOW
                    | _O_BINARY,
                    0o600,
                )
                while chunk := os.read(source_fd, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(safe_fd, view):]
                os.fsync(safe_fd)
                if not _markers_match(root_marker, _checked_directory(work_root, "媒体暂存目录在复制时已变化")):
                    raise WeiboError("媒体暂存目录在复制时已变化", kind=WeiboErrorKind.API)
            except OSError as exc:
                raise WeiboError("安全复制媒体暂存文件失败", original=exc) from exc
            finally:
                if safe_fd is not None:
                    os.close(safe_fd)
                os.close(source_fd)
            return safe_path
        parts = relative.parts
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        descriptors = [os.open(work_root, flags)]
        source_fd: int | None = None
        safe_fd: int | None = None
        safe_name = f"promotion-safe-{index}-{uuid.uuid4().hex}"
        try:
            if not _opened_matches(work_root.lstat(), descriptors[0]):
                raise WeiboError("媒体暂存目录在打开时已变化", kind=WeiboErrorKind.API)
            for component in parts[:-1]:
                parent_fd = descriptors[-1]
                marker = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
                    raise WeiboError("媒体暂存父目录不安全", kind=WeiboErrorKind.API)
                opened_parent = os.open(component, flags, dir_fd=parent_fd)
                if not _opened_matches(marker, opened_parent):
                    os.close(opened_parent)
                    raise WeiboError("媒体暂存父目录在打开时已变化", kind=WeiboErrorKind.API)
                descriptors.append(opened_parent)
            parent_fd = descriptors[-1]
            marker = os.stat(
                parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
                raise WeiboError("媒体暂存文件类型不安全", kind=WeiboErrorKind.API)
            if marker.st_nlink != 1:
                raise WeiboError("媒体暂存文件不能是硬链接", kind=WeiboErrorKind.API)
            source_fd = os.open(
                parts[-1],
                os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
                dir_fd=parent_fd,
            )
            opened = os.fstat(source_fd)
            expected = (
                marker.st_dev,
                marker.st_ino,
                marker.st_size,
                marker.st_mtime_ns,
                marker.st_nlink,
            )
            actual = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_nlink,
            )
            if actual != expected or opened.st_nlink != 1:
                raise WeiboError("媒体暂存文件在打开时已变化", kind=WeiboErrorKind.API)

            safe_fd = os.open(
                safe_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _O_NOFOLLOW
                | _O_BINARY,
                0o600,
                dir_fd=descriptors[0],
            )
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(safe_fd, view)
                    view = view[written:]
            os.fsync(safe_fd)
            os.fsync(descriptors[0])
        except OSError as exc:
            raise WeiboError("安全复制媒体暂存文件失败", original=exc) from exc
        finally:
            if safe_fd is not None:
                os.close(safe_fd)
            if source_fd is not None:
                os.close(source_fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return work_root / safe_name

    def _apply_promotion(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        promotion: dict[str, object],
    ) -> None:
        if not _SUPPORTS_DIRECTORY_FDS:
            self._apply_promotion_checked_paths(
                repository, run_id, checkpoint, promotion
            )
            return
        root = repository._root
        for entry in self._promotion_entries(promotion, run_id):
            target = entry["target"]
            staged_path = root / entry["staged"]
            backup_path = root / entry["backup"]
            if self._install_proof(staged_path) != entry["install_proof"]:
                raise WeiboError("媒体安装源与已持久化内容证明不一致", kind=WeiboErrorKind.API)
            with self._open_media_parent(root, target) as (
                parent_fd,
                target_name,
            ):
                if entry["expected_target"]["state"] == "present":
                    backup_fd = os.open(
                        backup_path.parent,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                    )
                    try:
                        os.rename(
                            target_name,
                            backup_path.name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=backup_fd,
                        )
                        os.fsync(parent_fd)
                        os.fsync(backup_fd)
                        captured = os.stat(
                            backup_path.name,
                            dir_fd=backup_fd,
                            follow_symlinks=False,
                        )
                        captured_state = self._target_state(captured)
                        if captured_state != entry["expected_target"]:
                            self._return_backup_without_overwrite(
                                backup_fd,
                                backup_path.name,
                                parent_fd,
                                target_name,
                            )
                            raise WeiboError(
                                "媒体目标在原子夺取时已变化，已原样放回",
                                kind=WeiboErrorKind.API,
                            )
                        entry["step"] = "backup_captured"
                        self._persist_promotion_step(
                            repository, run_id, checkpoint, promotion
                        )
                    finally:
                        os.close(backup_fd)
                self._install_staged_without_overwrite(
                    root / target,
                    staged_path,
                    parent_fd,
                    target_name,
                )
                installed = os.stat(
                    target_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                installed_target = self._target_state(installed)
                if os.name != "nt":
                    installed_target["nlink"] = installed.st_nlink - 1
                entry["installed_target"] = installed_target
                entry["step"] = "installed"
                self._persist_promotion_step(
                    repository, run_id, checkpoint, promotion
                )
                try:
                    staged_path.unlink()
                except FileNotFoundError:
                    pass
                os.fsync(parent_fd)
                _fsync_directory(staged_path.parent)

    def _apply_promotion_checked_paths(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        promotion: dict[str, object],
    ) -> None:
        root = repository._root
        for entry in self._promotion_entries(promotion, run_id):
            staged_path = root / entry["staged"]
            backup_path = root / entry["backup"]
            target_path = self._checked_media_target_path(root, entry["target"])
            if self._install_proof(staged_path) != entry["install_proof"]:
                raise WeiboError("媒体安装源与已持久化内容证明不一致", kind=WeiboErrorKind.API)
            self._assert_expected_target_path(target_path, entry["expected_target"])
            if entry["expected_target"]["state"] == "present":
                try:
                    backup_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise WeiboError("媒体提升备份路径已存在", kind=WeiboErrorKind.API)
                backup_parent = _checked_directory(
                    backup_path.parent, "媒体提升备份目录在打开时已变化"
                )
                os.replace(target_path, backup_path)
                if not _markers_match(
                    backup_parent,
                    _checked_directory(backup_path.parent, "媒体提升备份目录在移动时已变化"),
                ):
                    raise WeiboError("媒体提升备份目录在移动时已变化", kind=WeiboErrorKind.API)
                captured_state = self._target_state(backup_path.lstat())
                if captured_state != entry["expected_target"]:
                    self._return_backup_without_overwrite_path(backup_path, target_path)
                    raise WeiboError(
                        "媒体目标在原子夺取时已变化，已原样放回",
                        kind=WeiboErrorKind.API,
                    )
                entry["step"] = "backup_captured"
                self._persist_promotion_step(
                    repository, run_id, checkpoint, promotion
                )
            self._install_staged_without_overwrite(
                target_path, staged_path, None, target_path.name
            )
            entry["installed_target"] = self._target_state(target_path.lstat())
            entry["step"] = "installed"
            self._persist_promotion_step(repository, run_id, checkpoint, promotion)
            try:
                staged_path.unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(staged_path.parent)

    @staticmethod
    def _target_state(marker: os.stat_result) -> dict[str, object]:
        return {
            "state": "present",
            "dev": marker.st_dev,
            "ino": marker.st_ino,
            "mode": marker.st_mode,
            "nlink": marker.st_nlink,
            "size": marker.st_size,
            "mtime_ns": marker.st_mtime_ns,
        }

    @staticmethod
    def _persist_promotion_step(
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        promotion: dict[str, object],
    ) -> None:
        updated = dict(checkpoint)
        updated["promotion"] = promotion
        repository.update_sync_checkpoint(run_id, updated)
        checkpoint.clear()
        checkpoint.update(updated)

    @staticmethod
    def _return_backup_without_overwrite(
        backup_fd: int,
        backup_name: str,
        parent_fd: int,
        target_name: str,
    ) -> None:
        backup_marker = os.stat(
            backup_name,
            dir_fd=backup_fd,
            follow_symlinks=False,
        )
        if stat.S_ISDIR(backup_marker.st_mode):
            try:
                os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(
                    backup_name,
                    target_name,
                    src_dir_fd=backup_fd,
                    dst_dir_fd=parent_fd,
                )
                os.fsync(parent_fd)
                os.fsync(backup_fd)
                return
            raise WeiboError(
                "媒体目标已被第三方新建，目录对象已保留在恢复备份，请重试",
                kind=WeiboErrorKind.API,
            )
        try:
            os.link(
                backup_name,
                target_name,
                src_dir_fd=backup_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise WeiboError(
                "媒体目标已被第三方新建，原对象已保留在恢复备份，请重试",
                kind=WeiboErrorKind.API,
                original=exc,
            ) from exc
        os.unlink(backup_name, dir_fd=backup_fd)
        os.fsync(parent_fd)
        os.fsync(backup_fd)

    @staticmethod
    def _return_backup_without_overwrite_path(
        backup_path: Path,
        target_path: Path,
    ) -> None:
        backup_marker = backup_path.lstat()
        try:
            target_path.lstat()
        except FileNotFoundError:
            target_exists = False
        else:
            target_exists = True
        if target_exists:
            raise WeiboError(
                "媒体目标已被第三方新建，原对象已保留在恢复备份，请重试",
                kind=WeiboErrorKind.API,
            )
        if stat.S_ISDIR(backup_marker.st_mode):
            os.replace(backup_path, target_path)
            return
        try:
            os.link(backup_path, target_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise WeiboError(
                "媒体目标已被第三方新建，原对象已保留在恢复备份，请重试",
                kind=WeiboErrorKind.API,
                original=exc,
            ) from exc
        backup_path.unlink()

    @staticmethod
    def _install_staged_without_overwrite(
        target_path: Path,
        staged_path: Path,
        parent_fd: int | None,
        target_name: str,
    ) -> None:
        if _SUPPORTS_DIRECTORY_FDS and os.name != "nt":
            if parent_fd is None:
                raise WeiboError("媒体目标目录描述符缺失", kind=WeiboErrorKind.API)
            staged_fd = os.open(
                staged_path.parent,
                os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
            )
            try:
                try:
                    os.link(
                        staged_path.name,
                        target_name,
                        src_dir_fd=staged_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise WeiboError(
                        "媒体目标在原子安装时已被新建，请重试",
                        kind=WeiboErrorKind.API,
                        original=exc,
                    ) from exc
                os.fsync(parent_fd)
                os.fsync(staged_fd)
            finally:
                os.close(staged_fd)
            return

        source_fd = os.open(
            staged_path,
            os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
        )
        target_fd: int | None = None
        created = False
        try:
            target_fd = os.open(
                target_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _O_NOFOLLOW
                | _O_BINARY,
                0o600,
            )
            created = True
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target_fd, view):]
            os.fsync(target_fd)
        except FileExistsError as exc:
            raise WeiboError(
                "媒体目标在原子安装时已被新建，请重试",
                kind=WeiboErrorKind.API,
                original=exc,
            ) from exc
        except BaseException:
            if target_fd is not None:
                os.close(target_fd)
                target_fd = None
            if created:
                try:
                    target_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)

    def _restore_promotion(
        self,
        root: Path,
        run_id: str,
        promotion: dict[str, object],
    ) -> None:
        if not _SUPPORTS_DIRECTORY_FDS:
            self._restore_promotion_checked_paths(root, run_id, promotion)
            return
        for entry in reversed(self._promotion_entries(promotion, run_id)):
            staged_path = root / entry["staged"]
            backup_path = root / entry["backup"]
            with self._open_media_parent(root, entry["target"]) as (
                parent_fd,
                target_name,
            ):
                backup_exists = backup_path.is_file()
                staged_exists = staged_path.is_file()
                if backup_exists:
                    backup_fd = os.open(
                        backup_path.parent,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                    )
                    try:
                        marker = os.stat(
                            target_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        marker = None
                    installed_target = entry["installed_target"]
                    if marker is not None and (
                        not self._installed_by_us(
                            parent_fd,
                            target_name,
                            staged_path,
                            entry["install_proof"],
                            installed_target,
                        )
                    ):
                        os.close(backup_fd)
                        raise WeiboError(
                            "媒体目标已被第三方新建，恢复备份已保留，请重试",
                            kind=WeiboErrorKind.API,
                        )
                    if marker is not None:
                        os.unlink(target_name, dir_fd=parent_fd)
                    try:
                        self._return_backup_without_overwrite(
                            backup_fd,
                            backup_path.name,
                            parent_fd,
                            target_name,
                        )
                    finally:
                        os.close(backup_fd)
                elif entry["expected_target"]["state"] == "missing":
                    try:
                        marker = os.stat(
                            target_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        marker = None
                    if marker is not None:
                        if not self._installed_by_us(
                            parent_fd,
                            target_name,
                            staged_path,
                            entry["install_proof"],
                            entry["installed_target"],
                        ):
                            raise WeiboError(
                                "媒体目标已被第三方新建，恢复日志已保留，请重试",
                                kind=WeiboErrorKind.API,
                            )
                        os.unlink(target_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            try:
                staged_path.unlink()
            except FileNotFoundError:
                pass

    def _restore_promotion_checked_paths(
        self,
        root: Path,
        run_id: str,
        promotion: dict[str, object],
    ) -> None:
        for entry in reversed(self._promotion_entries(promotion, run_id)):
            staged_path = root / entry["staged"]
            backup_path = root / entry["backup"]
            target_path = self._checked_media_target_path(root, entry["target"])
            try:
                backup_marker = backup_path.lstat()
            except FileNotFoundError:
                backup_marker = None
            try:
                target_path.lstat()
            except FileNotFoundError:
                target_exists = False
            else:
                target_exists = True
            if backup_marker is not None:
                if target_exists and not self._installed_by_us_path(
                    target_path,
                    staged_path,
                    entry["install_proof"],
                    entry["installed_target"],
                ):
                    raise WeiboError(
                        "媒体目标已被第三方新建，恢复备份已保留，请重试",
                        kind=WeiboErrorKind.API,
                    )
                if target_exists:
                    target_path.unlink()
                self._return_backup_without_overwrite_path(backup_path, target_path)
            elif entry["expected_target"]["state"] == "missing" and target_exists:
                if not self._installed_by_us_path(
                    target_path,
                    staged_path,
                    entry["install_proof"],
                    entry["installed_target"],
                ):
                    raise WeiboError(
                        "媒体目标已被第三方新建，恢复日志已保留，请重试",
                        kind=WeiboErrorKind.API,
                    )
                target_path.unlink()
            try:
                staged_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _installed_by_us(
        parent_fd: int,
        target_name: str,
        staged_path: Path,
        proof: dict[str, object],
        installed_target: dict[str, object] | None,
    ) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target_name,
                os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
                dir_fd=parent_fd,
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink not in {1, 2}
                or before.st_size != proof["size"]
            ):
                return False
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
            if (
                any(getattr(before, field) != getattr(after, field) for field in fields)
                or digest.hexdigest() != proof["sha256"]
            ):
                return False

            staged_marker: os.stat_result | None = None
            try:
                staged_marker = staged_path.lstat()
            except FileNotFoundError:
                pass
            if staged_marker is not None and (
                stat.S_ISLNK(staged_marker.st_mode)
                or not stat.S_ISREG(staged_marker.st_mode)
            ):
                return False

            if installed_target is not None:
                exact_fields = {"dev", "ino", "mode", "size", "mtime_ns"}
                actual = PersonalArchiveSync._target_state(before)
                if any(actual[field] != installed_target[field] for field in exact_fields):
                    return False
                if before.st_nlink == installed_target["nlink"]:
                    return True
                if (
                    os.name != "nt"
                    and before.st_nlink == 2
                    and installed_target["nlink"] == 1
                    and staged_marker is not None
                    and (before.st_dev, before.st_ino)
                    == (staged_marker.st_dev, staged_marker.st_ino)
                ):
                    return True
                return False

            if os.name != "nt" and staged_marker is not None:
                return (
                    (before.st_dev, before.st_ino)
                    == (staged_marker.st_dev, staged_marker.st_ino)
                    and (staged_marker.st_dev, staged_marker.st_ino)
                    == (proof["staged_dev"], proof["staged_ino"])
                )
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _installed_by_us_path(
        target_path: Path,
        staged_path: Path,
        proof: dict[str, object],
        installed_target: dict[str, object] | None,
    ) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target_path,
                os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
            )
            before = os.fstat(descriptor)
            current = target_path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != proof["size"]
                or (before.st_dev, before.st_ino, before.st_mode)
                != (current.st_dev, current.st_ino, current.st_mode)
            ):
                return False
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
            if (
                any(getattr(before, field) != getattr(after, field) for field in fields)
                or digest.hexdigest() != proof["sha256"]
            ):
                return False
            if installed_target is not None:
                actual = PersonalArchiveSync._target_state(before)
                return actual == installed_target
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _discard_promotion_work(
        self,
        root: Path,
        run_id: str,
        promotion: dict[str, object],
    ) -> None:
        changed_directories: set[Path] = set()
        for entry in self._promotion_entries(promotion, run_id):
            for field in ("staged", "backup"):
                path = root / entry[field]
                try:
                    path.unlink()
                    changed_directories.add(path.parent)
                except FileNotFoundError:
                    pass
        for directory in changed_directories:
            _fsync_directory(directory)

    def _finish_promotion(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
        promotion: dict[str, object],
    ) -> None:
        self._discard_promotion_work(repository._root, run_id, promotion)
        cleared = dict(checkpoint)
        cleared["promotion"] = None
        repository.update_sync_checkpoint(run_id, cleared)
        checkpoint.clear()
        checkpoint.update(cleared)

    def _recover_promotion(
        self,
        repository: ArchiveRepository,
        run_id: str,
        checkpoint: dict[str, object],
    ) -> None:
        promotion = checkpoint.get("promotion")
        if not isinstance(promotion, dict):
            raise WeiboError("媒体提升恢复日志损坏", kind=WeiboErrorKind.PARSE)
        phase = promotion.get("phase")
        if phase == "db_committed":
            self._discard_promotion_work(repository._root, run_id, promotion)
        elif phase in {"prepared", "promoted"}:
            self._restore_promotion(repository._root, run_id, promotion)
        else:
            raise WeiboError("媒体提升恢复阶段无效", kind=WeiboErrorKind.PARSE)
        cleared = dict(checkpoint)
        cleared["promotion"] = None
        repository.update_sync_checkpoint(run_id, cleared)
        checkpoint.clear()
        checkpoint.update(cleared)

    @staticmethod
    def _promotion_entries(
        promotion: dict[str, object],
        expected_run_id: str,
    ) -> list[dict[str, object]]:
        if (
            set(promotion) != {"run_id", "phase", "entries"}
            or promotion.get("run_id") != expected_run_id
        ):
            raise WeiboError("媒体提升恢复日志 run_id 不匹配", kind=WeiboErrorKind.API)
        entries = promotion.get("entries")
        if not isinstance(entries, list) or not entries:
            raise WeiboError("媒体提升恢复日志条目损坏", kind=WeiboErrorKind.PARSE)
        required = {
            "staged", "target", "backup", "expected_target",
            "installed_target", "install_proof", "step",
        }
        targets: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != required:
                raise WeiboError("媒体提升恢复日志条目损坏", kind=WeiboErrorKind.PARSE)
            if not all(
                isinstance(entry[field], str) and bool(entry[field])
                for field in ("staged", "target", "backup")
            ):
                raise WeiboError("媒体提升恢复日志字段损坏", kind=WeiboErrorKind.PARSE)
            expected_target = entry["expected_target"]
            if not isinstance(expected_target, dict):
                raise WeiboError("媒体提升恢复日志目标状态损坏", kind=WeiboErrorKind.PARSE)
            numeric = {"dev", "ino", "mode", "nlink", "size", "mtime_ns"}
            state = expected_target.get("state")
            if state == "missing":
                if set(expected_target) != {"state"}:
                    raise WeiboError("媒体提升恢复日志目标状态损坏", kind=WeiboErrorKind.PARSE)
            elif state == "present":
                if set(expected_target) != {"state", *numeric} or any(
                    not isinstance(expected_target[field], int)
                    or isinstance(expected_target[field], bool)
                    for field in numeric
                ):
                    raise WeiboError("媒体提升恢复日志目标状态损坏", kind=WeiboErrorKind.PARSE)
            else:
                raise WeiboError("媒体提升恢复日志目标状态损坏", kind=WeiboErrorKind.PARSE)
            installed_target = entry["installed_target"]
            if installed_target is not None:
                if (
                    not isinstance(installed_target, dict)
                    or installed_target.get("state") != "present"
                    or set(installed_target) != {"state", *numeric}
                    or any(
                        not isinstance(installed_target[field], int)
                        or isinstance(installed_target[field], bool)
                        for field in numeric
                    )
                ):
                    raise WeiboError("媒体提升恢复日志安装状态损坏", kind=WeiboErrorKind.PARSE)
            install_proof = entry["install_proof"]
            if (
                not isinstance(install_proof, dict)
                or set(install_proof)
                != {"sha256", "size", "staged_dev", "staged_ino"}
                or not isinstance(install_proof["sha256"], str)
                or len(install_proof["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in install_proof["sha256"])
                or not isinstance(install_proof["size"], int)
                or isinstance(install_proof["size"], bool)
                or install_proof["size"] < 0
                or any(
                    value is not None
                    and (not isinstance(value, int) or isinstance(value, bool) or value < 0)
                    for value in (
                        install_proof["staged_dev"],
                        install_proof["staged_ino"],
                    )
                )
                or (install_proof["staged_dev"] is None)
                != (install_proof["staged_ino"] is None)
                or (
                    os.name != "nt"
                    and install_proof["staged_dev"] is None
                )
            ):
                raise WeiboError("媒体提升恢复日志内容证明损坏", kind=WeiboErrorKind.PARSE)
            if entry["step"] not in {"prepared", "backup_captured", "installed"}:
                raise WeiboError("媒体提升恢复日志步骤损坏", kind=WeiboErrorKind.PARSE)
            for field in ("staged", "target", "backup"):
                value = entry[field]
                parts = value.split("/")
                if (
                    "\\" in value
                    or Path(value).is_absolute()
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise WeiboError("媒体提升恢复日志路径不安全", kind=WeiboErrorKind.API)
            staged_parts = entry["staged"].split("/")
            backup_parts = entry["backup"].split("/")
            if (
                len(staged_parts) < 3
                or len(backup_parts) < 3
                or staged_parts[:2] != [".work", expected_run_id]
                or backup_parts[:2] != [".work", expected_run_id]
            ):
                raise WeiboError("媒体提升恢复日志暂存路径不安全", kind=WeiboErrorKind.API)
            PersonalArchiveSync._validate_media_target(entry["target"])
            if entry["target"] in targets:
                raise WeiboError("媒体提升恢复日志目标路径重复", kind=WeiboErrorKind.API)
            targets.add(entry["target"])
        return entries

    @staticmethod
    def _assert_expected_target(
        parent_fd: int,
        target_name: str,
        expected: dict[str, object],
    ) -> None:
        try:
            marker = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            marker = None
        if expected["state"] == "missing":
            if marker is not None:
                raise WeiboError("媒体目标在提升前已被新建", kind=WeiboErrorKind.API)
            return
        if marker is None:
            raise WeiboError("媒体目标在提升前已消失", kind=WeiboErrorKind.API)
        actual = {
            "state": "present",
            "dev": marker.st_dev,
            "ino": marker.st_ino,
            "mode": marker.st_mode,
            "nlink": marker.st_nlink,
            "size": marker.st_size,
            "mtime_ns": marker.st_mtime_ns,
        }
        if actual != expected:
            raise WeiboError("媒体目标在提升前已变化", kind=WeiboErrorKind.API)

    @staticmethod
    def _assert_expected_target_path(
        target_path: Path,
        expected: dict[str, object],
    ) -> None:
        try:
            marker = target_path.lstat()
        except FileNotFoundError:
            marker = None
        if expected["state"] == "missing":
            if marker is not None:
                raise WeiboError("媒体目标在提升前已被新建", kind=WeiboErrorKind.API)
            return
        if marker is None:
            raise WeiboError("媒体目标在提升前已消失", kind=WeiboErrorKind.API)
        if PersonalArchiveSync._target_state(marker) != expected:
            raise WeiboError("媒体目标在提升前已变化", kind=WeiboErrorKind.API)

    @staticmethod
    def _validate_media_target(value: str) -> None:
        if media_path_shape(value) is None:
            raise WeiboError("媒体目标必须是 media/ 下的 POSIX 相对路径", kind=WeiboErrorKind.API)

    def _checked_media_target_path(self, root: Path, relative_path: str) -> Path:
        self._validate_media_target(relative_path)
        root_marker = _checked_directory(root, "归档目录在打开时已变化")
        parent = root
        markers = [root_marker]
        for component in relative_path.split("/")[:-1]:
            child = parent / component
            try:
                child.mkdir(mode=0o700)
            except FileExistsError:
                pass
            marker = _checked_directory(child, "媒体目录不能是符号链接")
            markers.append(marker)
            parent = child
        current_paths = [root]
        parent = root
        for component in relative_path.split("/")[:-1]:
            parent = parent / component
            current_paths.append(parent)
        if any(
            not _markers_match(marker, _checked_directory(path, "媒体目录在打开时已变化"))
            for path, marker in zip(current_paths, markers)
        ):
            raise WeiboError("媒体目录在打开时已变化", kind=WeiboErrorKind.API)
        return parent / relative_path.split("/")[-1]

    @contextmanager
    def _open_media_parent(
        self, root: Path, relative_path: str
    ) -> Iterator[tuple[int, str]]:
        parts = relative_path.split("/")
        if media_path_shape(relative_path) is None:
            raise WeiboError("媒体目标路径不安全", kind=WeiboErrorKind.API)
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        descriptors = [os.open(root, flags)]
        try:
            if not _opened_matches(root.lstat(), descriptors[0]):
                raise WeiboError("归档目录在打开时已变化", kind=WeiboErrorKind.API)
            for component in parts[:-1]:
                parent_fd = descriptors[-1]
                try:
                    marker = os.stat(
                        component,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    os.mkdir(component, dir_fd=parent_fd)
                    marker = os.stat(
                        component,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                if stat.S_ISLNK(marker.st_mode):
                    raise WeiboError(
                        "媒体目录不能是符号链接",
                        kind=WeiboErrorKind.API,
                    )
                if not stat.S_ISDIR(marker.st_mode):
                    raise WeiboError(
                        "媒体目录路径不是目录",
                        kind=WeiboErrorKind.API,
                    )
                try:
                    opened_parent = os.open(component, flags, dir_fd=parent_fd)
                    if not _opened_matches(marker, opened_parent):
                        os.close(opened_parent)
                        raise WeiboError("媒体目录在打开时已变化", kind=WeiboErrorKind.API)
                    descriptors.append(opened_parent)
                except OSError as exc:
                    raise WeiboError(
                        "无法安全打开媒体目录",
                        kind=WeiboErrorKind.API,
                        original=exc,
                    ) from exc
            yield descriptors[-1], parts[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _check_cancelled(self) -> None:
        if self.cancel_requested():
            raise OperationCancelled("任务已取消")
        if self.pause_requested():
            raise OperationPaused("任务已暂停")
