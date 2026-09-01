"""微博书 - 媒体文件下载模块

多线程下载微博中的图片、视频和实况照片。
支持图片清晰度选择、实况照片音视频分离、视频封面下载。
"""

from __future__ import annotations

import logging
import hashlib
import os
import re
import secrets
import stat
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from tqdm import tqdm

from .post_converter import transform_image_url
from .errors import OperationCancelled, OperationPaused, WeiboError, WeiboErrorKind
from .models import Comment, ImageQuality, MediaType, Post, PostMedia

logger = logging.getLogger(__name__)


def _writable_console_stream():
    """返回 tqdm 可安全写入的控制台流；frozen 无控制台时返回 None。

    注意：frozen Windows console=False 下 sys.stderr/sys.stdout 可能存在"假流"
    （非 None、closed=False、write("") 不抛异常），但 tqdm status_printer 内部
    fp.write('\\r...') 或 fp.flush() 仍会抛 ValueError: I/O operation on closed file。
    因此 frozen 模式应在调用前直接禁用 tqdm，不依赖此函数的流检测。
    """
    for stream in (sys.stderr, sys.stdout):
        if stream is None or getattr(stream, "closed", False):
            continue
        try:
            stream.write("")
        except (AttributeError, OSError, ValueError):
            continue
        return stream
    return None


def _media_progress(total: int):
    """frozen Windows console=False 时禁用 tqdm 控制台进度条，保留结构化进度事件。

    frozen 模式（PyInstaller 打包）下没有真实控制台，tqdm 进度条用户看不到
    （前端通过 WebSocket 推进度），禁用不影响任何可见功能。直接 disable=True，
    不依赖 _writable_console_stream() 的流检测，避免"假流"导致 tqdm 崩溃。
    """
    if getattr(sys, "frozen", False):
        return tqdm(total=total, desc="下载媒体", unit="个", disable=True)
    stream = _writable_console_stream()
    if stream is None:
        return tqdm(total=total, desc="下载媒体", unit="个", disable=True)
    return tqdm(total=total, desc="下载媒体", unit="个", file=stream)


WEIBO_MEDIA_HOST_ROOTS = (
    "weibo.com",
    "weibo.cn",
    "sina.com.cn",
    "sina.cn",
    "sinaimg.cn",
)


def download_headers_for_url(url: str) -> dict[str, str]:
    """只对微博/新浪媒体域发送微博 Referer，外站保持无 Referer。"""
    host = (urlparse(url).hostname or "").rstrip(".").lower()
    if any(host == root or host.endswith(f".{root}") for root in WEIBO_MEDIA_HOST_ROOTS):
        return {"Referer": "https://weibo.com/"}
    return {}


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """清理文件名，移除非法字符"""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    if len(name) > max_len:
        base, ext = os.path.splitext(name)
        name = base[:max_len] + ext
    return name.strip("._")


def get_extension(url: str, media_type: MediaType) -> str:
    """从 URL 推断文件扩展名"""
    parsed = urlparse(url)
    inspected_url = parsed.path
    livephoto = parse_qs(parsed.query).get("livephoto")
    if livephoto:
        inspected_url = urlparse(unquote(livephoto[0])).path
    ext_match = re.search(r"\.(jpg|jpeg|png|gif|webp|heic|heif|mp4|mov|avi)$", inspected_url, re.I)
    if ext_match:
        return ext_match.group(1).lower()

    ext_map = {
        MediaType.VIDEO: "mp4",
        MediaType.LIVE_PHOTO: "mp4",
    }
    return ext_map.get(media_type, "jpg")


def download_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    timeout: int = 120,
    max_retries: int = 2,
    cancel_event: Optional[Event] = None,
    expected_image_extension: str | None = None,
    destination_dir_fd: int | _CheckedDirectory | None = None,
    destination_name: str | None = None,
    resolved_destination_name: list[str] | None = None,
    before_request: Callable[[], None] | None = None,
    request_completed: Callable[[], None] | None = None,
    retry_wait: Callable[[float], None] | None = None,
    raise_request_errors: bool = False,
) -> bool:
    """下载单个文件

    Args:
        client: httpx 客户端
        url: 下载 URL
        dest: 目标路径
        timeout: 超时秒数
        max_retries: 重试次数

    Returns:
        bool: 是否下载成功
    """
    if destination_dir_fd is not None:
        if not destination_name:
            raise WeiboError(
                "评论图片目标文件名缺失",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        resolved_name = _download_file_at(
            client,
            url,
            dest,
            destination_dir_fd,
            destination_name,
            timeout=timeout,
            max_retries=max_retries,
            cancel_event=cancel_event,
            expected_image_extension=expected_image_extension,
            before_request=before_request,
            request_completed=request_completed,
            retry_wait=retry_wait,
            raise_request_errors=raise_request_errors,
        )
        if resolved_name is not None and resolved_destination_name is not None:
            resolved_destination_name.append(resolved_name)
        return resolved_name is not None

    if dest.exists() and dest.stat().st_size > 0:
        return True

    for attempt in range(max_retries + 1):
        temp_path: Path | None = None
        request_started = False
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("任务已取消")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            if before_request is not None:
                before_request()
            request_started = True
            with client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=timeout,
                headers=download_headers_for_url(url),
            ) as resp:
                if not 200 <= resp.status_code < 300 or resp.status_code == 204:
                    _raise_pacing_http_error(resp.status_code, raise_request_errors)
                    logger.debug("媒体 HTTP 状态不可用: %s → %s", url, resp.status_code)
                    return False
                resp.raise_for_status()
                if not _response_matches_image_extension(resp, expected_image_extension):
                    logger.warning("评论图片响应类型与扩展名不匹配: %s", url)
                    return False
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=dest.parent,
                    prefix=f".{dest.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    temp_path = Path(f.name)
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if cancel_event is not None and cancel_event.is_set():
                            raise OperationCancelled("任务已取消")
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            if written <= 0:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                    temp_path = None
                return False
            os.replace(temp_path, dest)
            temp_path = None
            _fsync_directory(dest.parent)
            return True
        except OperationCancelled:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        except WeiboError:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if request_started and request_completed is not None:
                request_completed()
                request_started = False
            if attempt < max_retries:
                if retry_wait is not None:
                    retry_wait(1.0)
                else:
                    import time as _time
                    _time.sleep(1)
            else:
                if raise_request_errors:
                    raise WeiboError(
                        "媒体下载网络请求失败",
                        kind=WeiboErrorKind.NETWORK,
                        original=exc,
                    ) from exc
                return False
        finally:
            if request_started and request_completed is not None:
                request_completed()
    return False


_IMAGE_CONTENT_TYPES = {
    "jpg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "jpeg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "png": {"image/png"},
    "gif": {"image/gif"},
    "webp": {"image/webp"},
    "heic": {"image/heic", "image/heif"},
    "heif": {"image/heic", "image/heif"},
}

_IMAGE_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}

_DYNAMIC_IMAGE_EXTENSIONS = frozenset(_IMAGE_MIME_EXTENSIONS.values())
_SUPPORTS_DIRECTORY_FDS = (
    os.name != "nt"
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)

_VERIFIED_IMAGE_EXTENSION_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|heic|heif)$", re.I
)


def _response_matches_image_extension(response, extension: str | None) -> bool:
    if extension is None:
        return True
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not content_type.startswith("image/"):
        return False
    return content_type in _IMAGE_CONTENT_TYPES.get(extension.lower(), set())


def _resolved_image_extension(response, expected_extension: str | None) -> str | None:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if expected_extension is not None:
        if content_type in _IMAGE_CONTENT_TYPES.get(expected_extension.lower(), set()):
            return expected_extension.lower()
        return None
    return _IMAGE_MIME_EXTENSIONS.get(content_type)


def _verified_image_extension_from_url(url: str) -> str | None:
    path = urlparse(url).path
    match = _VERIFIED_IMAGE_EXTENSION_RE.search(path)
    return match.group(1).lower() if match else None


def _fsync_directory(directory: Path) -> None:
    if not _SUPPORTS_DIRECTORY_FDS:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _CheckedDirectory:
    path: Path
    device: int
    inode: int
    mode: int


def _checked_directory(path: Path, message: str) -> _CheckedDirectory:
    marker = path.lstat()
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
        raise WeiboError(message, kind=WeiboErrorKind.PARSE, recoverable=False)
    return _CheckedDirectory(path, marker.st_dev, marker.st_ino, marker.st_mode)


def _checked_directory_path(reference: _CheckedDirectory) -> Path:
    current = _checked_directory(reference.path, "评论图片目录在操作时已变化")
    if (current.device, current.inode, current.mode) != (
        reference.device,
        reference.inode,
        reference.mode,
    ):
        raise WeiboError(
            "评论图片目录在操作时已变化",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    return reference.path


def _close_directory_reference(reference: int | _CheckedDirectory) -> None:
    if isinstance(reference, int):
        os.close(reference)


def _download_file_at(
    client: httpx.Client,
    url: str,
    display_dest: Path,
    directory_fd: int | _CheckedDirectory,
    destination_name: str,
    *,
    timeout: int,
    max_retries: int,
    cancel_event: Optional[Event],
    expected_image_extension: str | None,
    before_request: Callable[[], None] | None = None,
    request_completed: Callable[[], None] | None = None,
    retry_wait: Callable[[float], None] | None = None,
    raise_request_errors: bool = False,
) -> str | None:
    if isinstance(directory_fd, _CheckedDirectory):
        return _download_file_in_checked_directory(
            client,
            url,
            display_dest,
            directory_fd,
            destination_name,
            timeout=timeout,
            max_retries=max_retries,
            cancel_event=cancel_event,
            expected_image_extension=expected_image_extension,
            before_request=before_request,
            request_completed=request_completed,
            retry_wait=retry_wait,
            raise_request_errors=raise_request_errors,
        )
    if expected_image_extension is not None:
        suffix = f".{expected_image_extension.lower()}"
        destination_base = (
            destination_name[: -len(suffix)]
            if destination_name.lower().endswith(suffix)
            else destination_name
        )
    else:
        destination_base = destination_name
    for attempt in range(max_retries + 1):
        temp_name = f".{destination_base}.{secrets.token_hex(8)}.tmp"
        temp_created = False
        request_started = False
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("任务已取消")
        try:
            written = 0
            if before_request is not None:
                before_request()
            request_started = True
            with client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=timeout,
                headers=download_headers_for_url(url),
            ) as resp:
                if not 200 <= resp.status_code < 300 or resp.status_code == 204:
                    _raise_pacing_http_error(resp.status_code, raise_request_errors)
                    logger.debug("媒体 HTTP 状态不可用: %s → %s", url, resp.status_code)
                    return None
                resp.raise_for_status()
                resolved_extension = _resolved_image_extension(
                    resp, expected_image_extension
                )
                if resolved_extension is None:
                    logger.warning("评论图片响应类型与扩展名不匹配: %s", url)
                    return None
                final_name = f"{destination_base}.{resolved_extension}"
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                file_fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
                temp_created = True
                with os.fdopen(file_fd, "wb") as output:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if cancel_event is not None and cancel_event.is_set():
                            raise OperationCancelled("任务已取消")
                        if chunk:
                            output.write(chunk)
                            written += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if written <= 0:
                os.unlink(temp_name, dir_fd=directory_fd)
                temp_created = False
                return None
            if _inspect_secure_target_at(directory_fd, final_name):
                os.unlink(temp_name, dir_fd=directory_fd)
                temp_created = False
                return final_name
            os.replace(
                temp_name,
                final_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temp_created = False
            os.fsync(directory_fd)
            return final_name
        except OperationCancelled:
            if temp_created:
                os.unlink(temp_name, dir_fd=directory_fd)
            raise
        except WeiboError:
            if temp_created:
                os.unlink(temp_name, dir_fd=directory_fd)
            raise
        except Exception as exc:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            if request_started and request_completed is not None:
                request_completed()
                request_started = False
            if attempt < max_retries:
                if retry_wait is not None:
                    retry_wait(1.0)
                else:
                    import time as _time
                    _time.sleep(1)
            else:
                if raise_request_errors:
                    raise WeiboError(
                        "评论图片网络请求失败",
                        kind=WeiboErrorKind.NETWORK,
                        original=exc,
                    ) from exc
                logger.debug("评论图片下载失败: %s → %s", url, display_dest)
                return None
        finally:
            if request_started and request_completed is not None:
                request_completed()
    return None


def _download_file_in_checked_directory(
    client: httpx.Client,
    url: str,
    display_dest: Path,
    directory: _CheckedDirectory,
    destination_name: str,
    *,
    timeout: int,
    max_retries: int,
    cancel_event: Optional[Event],
    expected_image_extension: str | None,
    before_request: Callable[[], None] | None = None,
    request_completed: Callable[[], None] | None = None,
    retry_wait: Callable[[float], None] | None = None,
    raise_request_errors: bool = False,
) -> str | None:
    if expected_image_extension is not None:
        suffix = f".{expected_image_extension.lower()}"
        destination_base = (
            destination_name[: -len(suffix)]
            if destination_name.lower().endswith(suffix)
            else destination_name
        )
    else:
        destination_base = destination_name
    for attempt in range(max_retries + 1):
        temp_path: Path | None = None
        request_started = False
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("任务已取消")
        try:
            written = 0
            if before_request is not None:
                before_request()
            request_started = True
            with client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=timeout,
                headers=download_headers_for_url(url),
            ) as resp:
                if not 200 <= resp.status_code < 300 or resp.status_code == 204:
                    _raise_pacing_http_error(resp.status_code, raise_request_errors)
                    logger.debug("媒体 HTTP 状态不可用: %s → %s", url, display_dest)
                    return None
                resp.raise_for_status()
                resolved_extension = _resolved_image_extension(resp, expected_image_extension)
                if resolved_extension is None:
                    logger.warning("评论图片响应类型与扩展名不匹配: %s", url)
                    return None
                final_name = f"{destination_base}.{resolved_extension}"
                target_directory = _checked_directory_path(directory)
                temp_path = target_directory / f".{destination_base}.{secrets.token_hex(8)}.tmp"
                descriptor = os.open(
                    temp_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if cancel_event is not None and cancel_event.is_set():
                            raise OperationCancelled("任务已取消")
                        if chunk:
                            output.write(chunk)
                            written += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if written <= 0:
                temp_path.unlink(missing_ok=True)
                temp_path = None
                return None
            if _inspect_secure_target_at(directory, final_name):
                temp_path.unlink(missing_ok=True)
                temp_path = None
                return final_name
            target_directory = _checked_directory_path(directory)
            os.replace(temp_path, target_directory / final_name)
            temp_path = None
            _fsync_directory(target_directory)
            return final_name
        except OperationCancelled:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        except WeiboError:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if request_started and request_completed is not None:
                request_completed()
                request_started = False
            if attempt < max_retries:
                if retry_wait is not None:
                    retry_wait(1.0)
                else:
                    import time as _time
                    _time.sleep(1)
            else:
                if raise_request_errors:
                    raise WeiboError(
                        "评论图片网络请求失败",
                        kind=WeiboErrorKind.NETWORK,
                        original=exc,
                    ) from exc
                logger.debug("评论图片下载失败: %s → %s", url, display_dest)
                return None
        finally:
            if request_started and request_completed is not None:
                request_completed()
    return None


def _inspect_secure_target_at(directory_fd: int | _CheckedDirectory, name: str) -> bool:
    if isinstance(directory_fd, _CheckedDirectory):
        target = _checked_directory_path(directory_fd) / name
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(target_stat.st_mode):
            raise WeiboError(
                "评论图片目标是符号链接",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        if not stat.S_ISREG(target_stat.st_mode):
            raise WeiboError(
                "评论图片目标不是普通文件",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        if target_stat.st_nlink > 1:
            raise WeiboError(
                "评论图片目标是硬链接",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        if target_stat.st_size > 0:
            return True
        target.unlink()
        return False
    try:
        target_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(target_stat.st_mode):
        raise WeiboError(
            "评论图片目标是符号链接",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    if not stat.S_ISREG(target_stat.st_mode):
        raise WeiboError(
            "评论图片目标不是普通文件",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    if target_stat.st_nlink > 1:
        raise WeiboError(
            "评论图片目标是硬链接",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    if target_stat.st_size > 0:
        return True
    os.unlink(name, dir_fd=directory_fd)
    return False


def _raise_pacing_http_error(status_code: int, enabled: bool) -> None:
    if not enabled:
        return
    if status_code in {429, 432}:
        raise WeiboError("媒体请求受到限流", kind=WeiboErrorKind.RATE_LIMIT)
    if status_code == 401:
        raise WeiboError("媒体请求登录状态已失效", kind=WeiboErrorKind.AUTH)
    if 500 <= status_code < 600:
        raise WeiboError("媒体服务暂时不可用", kind=WeiboErrorKind.NETWORK)


class MediaDownloader:
    """媒体文件下载器

    Args:
        output_dir: 输出目录
        max_workers: 并发下载线程数
        image_quality: 图片清晰度
    """

    def __init__(
        self,
        output_dir: str | Path,
        max_workers: int = 5,
        image_quality: ImageQuality = ImageQuality.ORIGINAL,
        pacing_scheduler: Any | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.media_dir = self.output_dir / "media"
        self.max_workers = max_workers
        self.image_quality = image_quality
        self.pacing_scheduler = pacing_scheduler
        self._pacing_stop_error: OperationPaused | None = None
        self._progress_event_callback = None
        self._cancel_event: Optional[Event] = None

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled("任务已取消")

    def _uses_pacing(self) -> bool:
        return bool(
            self.pacing_scheduler is not None
            and self.pacing_scheduler.is_low_intensity
        )

    def _run_media_request(self, operation: Callable[[], Any]) -> Any:
        if not self._uses_pacing():
            return operation()
        if self._pacing_stop_error is not None:
            raise self._pacing_stop_error
        try:
            return self.pacing_scheduler.run("media", operation)
        except OperationPaused as exc:
            self._pacing_stop_error = exc
            raise

    def _download_media_file(
        self,
        client: httpx.Client,
        url: str,
        destination: Path,
    ) -> bool:
        if not self._uses_pacing():
            return download_file(
                client,
                url,
                destination,
                cancel_event=self._cancel_event,
            )
        return self._run_media_request(
            lambda: download_file(
                client,
                url,
                destination,
                max_retries=0,
                cancel_event=self._cancel_event,
                raise_request_errors=True,
            )
        )

    def _emit_progress_event(self, current: int, total: int, detail: str) -> None:
        callback = self._progress_event_callback
        if callback is not None:
            callback({"current": current, "total": total, "unit": "media", "detail": detail})

    def _build_media_filename(self, post: Post, media: PostMedia, idx: int) -> str:
        """根据媒体类型构建文件名"""
        bid = post.bid
        if media.type == MediaType.IMAGE:
            ext = get_extension(media.url, media.type)
            return f"{bid}_img_{idx + 1:02d}.{ext}"
        elif media.type == MediaType.VIDEO:
            ext = get_extension(media.url, media.type)
            return f"{bid}_video_{idx + 1:02d}.{ext}"
        elif media.type == MediaType.LIVE_PHOTO:
            ext = get_extension(media.url, media.type)
            return f"{bid}_live_{idx + 1:02d}.{ext}"
        return f"{bid}_media_{idx + 1:02d}.{get_extension(media.url, media.type)}"

    @staticmethod
    def _validate_comment_path_value(value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise WeiboError(
                f"评论图片{label}不安全",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )

    @staticmethod
    def _validate_comment_image_url(url: str) -> None:
        parsed = urlparse(url)
        path_segments = [unquote(segment) for segment in parsed.path.split("/")]
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(segment in {".", ".."} or "\\" in segment for segment in path_segments)
        ):
            raise WeiboError(
                "评论图片 URL 不安全",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )

    @staticmethod
    def _iter_comments(comments: list[Comment]):
        visited: set[int] = set()
        stack = list(reversed(comments))
        while stack:
            comment = stack.pop()
            identity = id(comment)
            if identity in visited:
                continue
            visited.add(identity)
            yield comment
            stack.extend(reversed(comment.replies))

    def _comment_image_destination(
        self, post_bid: str, comment: Comment
    ) -> tuple[Path, str | None]:
        self._validate_comment_path_value(post_bid, "微博 BID")
        self._validate_comment_path_value(comment.id, "评论 ID")
        self._validate_comment_image_url(comment.image_url)
        extension = _verified_image_extension_from_url(comment.image_url)
        filename = f"{post_bid}_{comment.id}"
        if extension is not None:
            filename = f"{filename}.{extension}"
        relative = Path("media") / "comments" / filename
        return self.output_dir / relative, extension

    def _open_secure_comment_directory(self) -> int | _CheckedDirectory:
        try:
            root_stat = os.lstat(self.output_dir)
        except FileNotFoundError:
            self.output_dir.mkdir(parents=True, exist_ok=False)
            root_stat = os.lstat(self.output_dir)
        if stat.S_ISLNK(root_stat.st_mode):
            self._raise_unsafe_storage("归档根目录是符号链接")
        if not stat.S_ISDIR(root_stat.st_mode):
            self._raise_unsafe_storage("归档根路径不是目录")

        if not _SUPPORTS_DIRECTORY_FDS:
            root_reference = _checked_directory(
                self.output_dir, "归档根目录是符号链接"
            )
            current_reference = root_reference
            for component in ("media", "comments"):
                current_path = _checked_directory_path(current_reference)
                child_path = current_path / component
                try:
                    child_path.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                current_reference = _checked_directory(
                    child_path, f"评论图片目录 {component} 是符号链接"
                )
            _checked_directory_path(root_reference)
            return current_reference

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            current_fd = os.open(self.output_dir, flags)
        except OSError as exc:
            raise WeiboError(
                "无法安全打开归档根目录",
                kind=WeiboErrorKind.PARSE,
                original=exc,
                recoverable=False,
            ) from exc

        try:
            for component in ("media", "comments"):
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                component_stat = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False
                )
                if stat.S_ISLNK(component_stat.st_mode):
                    self._raise_unsafe_storage(
                        f"评论图片目录 {component} 是符号链接"
                    )
                if not stat.S_ISDIR(component_stat.st_mode):
                    self._raise_unsafe_storage(
                        f"评论图片路径 {component} 不是目录"
                    )
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def download_avatar(self, url: str, identity: str) -> Path | None:
        """把精确用户头像 URL 下载为 MIME 验证后的本地图片。"""
        self._validate_comment_image_url(url)
        if not identity:
            raise WeiboError("头像身份标识缺失", kind=WeiboErrorKind.PARSE, recoverable=False)
        avatar_dir = self.media_dir / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.media_dir, avatar_dir):
            marker = os.lstat(directory)
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
                self._raise_unsafe_storage("头像目录不安全")
        base = hashlib.sha256(f"{identity}\0{url}".encode("utf-8")).hexdigest()[:24]
        existing = [path for path in avatar_dir.glob(f"{base}_*.*") if path.suffix[1:] in _DYNAMIC_IMAGE_EXTENSIONS]
        if len(existing) > 1:
            raise WeiboError("头像本地文件存在扩展名冲突", kind=WeiboErrorKind.PARSE, recoverable=False)
        if existing:
            marker = os.lstat(existing[0])
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                raise WeiboError("头像本地文件不安全", kind=WeiboErrorKind.PARSE, recoverable=False)
            if marker.st_size > 0:
                return existing[0]
        if _SUPPORTS_DIRECTORY_FDS:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd: int | _CheckedDirectory = os.open(avatar_dir, flags)
        else:
            directory_fd = _checked_directory(avatar_dir, "头像目录不安全")
        try:
            with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120.0)) as client:
                def download_avatar_file():
                    return _download_file_at(
                        client, url, avatar_dir / base, directory_fd, base,
                        timeout=120,
                        max_retries=0 if self._uses_pacing() else 2,
                        cancel_event=self._cancel_event,
                        expected_image_extension=None,
                        raise_request_errors=self._uses_pacing(),
                    )

                if self._uses_pacing():
                    self.pacing_scheduler.add_media_requests(1)
                resolved = self._run_media_request(download_avatar_file)
        finally:
            _close_directory_reference(directory_fd)
        if not resolved:
            return None
        downloaded = avatar_dir / resolved
        digest = hashlib.sha256()
        with downloaded.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        target = avatar_dir / f"{base}_{digest.hexdigest()}{downloaded.suffix.lower()}"
        if target.exists():
            marker = os.lstat(target)
            if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                self._raise_unsafe_storage("头像内容寻址文件不安全")
            downloaded.unlink()
        else:
            os.replace(downloaded, target)
        _fsync_directory(avatar_dir)
        return target

    @staticmethod
    def _raise_unsafe_storage(message: str) -> None:
        raise WeiboError(
            message,
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )

    def _inspect_comment_target(self, directory_fd: int | _CheckedDirectory, name: str) -> bool:
        return _inspect_secure_target_at(directory_fd, name)

    def _reuse_extensionless_comment_target(
        self,
        directory_fd: int | _CheckedDirectory,
        base_name: str,
        comments: list[Comment],
    ) -> str | None:
        trusted_names: set[str] = set()
        for comment in comments:
            local_image = comment.local_image
            if not isinstance(local_image, str) or not local_image:
                continue
            matched_name = None
            for extension in _DYNAMIC_IMAGE_EXTENSIONS:
                expected_name = f"{base_name}.{extension}"
                expected_path = f"media/comments/{expected_name}"
                if local_image == expected_path:
                    matched_name = expected_name
                    break
            if matched_name is None:
                continue
            if self._inspect_comment_target(directory_fd, matched_name):
                trusted_names.add(matched_name)

        if len(trusted_names) > 1:
            raise WeiboError(
                "评论图片旧路径存在多个扩展名冲突",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        if trusted_names:
            return next(iter(trusted_names))

        scanned_names: list[str] = []
        for extension in sorted(_DYNAMIC_IMAGE_EXTENSIONS):
            name = f"{base_name}.{extension}"
            if self._inspect_comment_target(directory_fd, name):
                scanned_names.append(name)
        if len(scanned_names) > 1:
            raise WeiboError(
                "评论图片目录存在多个扩展名文件冲突",
                kind=WeiboErrorKind.PARSE,
                recoverable=False,
            )
        return scanned_names[0] if scanned_names else None

    def _download_comment_with_owned_fd(
        self,
        client: httpx.Client,
        url: str,
        destination: Path,
        directory_fd: int,
        extension: str | None,
    ) -> str | None:
        try:
            resolved_names: list[str] = []
            def download_comment_file():
                return download_file(
                    client,
                    url,
                    destination,
                    cancel_event=self._cancel_event,
                    expected_image_extension=extension,
                    destination_dir_fd=directory_fd,
                    destination_name=destination.name,
                    resolved_destination_name=resolved_names,
                    max_retries=0 if self._uses_pacing() else 2,
                    raise_request_errors=self._uses_pacing(),
                )

            ok = self._run_media_request(download_comment_file)
            if not ok:
                return None
            return resolved_names[0] if resolved_names else destination.name
        finally:
            _close_directory_reference(directory_fd)

    def _count_pending_downloads(
        self,
        unique_tasks: list[tuple[str, Path, str]],
        comment_destinations: dict[str, dict],
    ) -> int:
        pending = 0
        for _url, destination, _bid in unique_tasks:
            destination_key = str(destination)
            if destination_key not in comment_destinations:
                if not (destination.exists() and destination.stat().st_size > 0):
                    pending += 1
                continue
            entry = comment_destinations[destination_key]
            directory_fd = self._open_secure_comment_directory()
            try:
                expected_extension = entry["expected_extension"]
                if (
                    expected_extension is not None
                    and self._inspect_comment_target(directory_fd, destination.name)
                ):
                    continue
                if expected_extension is None and self._reuse_extensionless_comment_target(
                    directory_fd,
                    destination.name,
                    entry["references"],
                ) is not None:
                    continue
                pending += 1
            finally:
                _close_directory_reference(directory_fd)
        return pending

    def download_all(self, posts: list[Post]) -> dict:
        """下载所有帖子的媒体文件（多线程）

        对实况照片：同时下载视频部分和图片静态部分
        对视频：同时下载封面图
        """
        self._check_cancelled()
        self._pacing_stop_error = None
        def iter_posts(items: list[Post]):
            for item in items:
                yield item
                if item.retweeted is not None:
                    yield from iter_posts([item.retweeted])

        all_posts = list(iter_posts(posts))
        total_media = sum(
            len(p.media)
            + (1 if p.link_card and p.link_card.image_url else 0)
            + sum(1 for comment in self._iter_comments(p.comments) if comment.image_url)
            for p in all_posts
        )
        if total_media == 0:
            if self._uses_pacing():
                self.pacing_scheduler.add_media_requests(0)
            logger.info("📁 没有媒体文件需要下载")
            self._emit_progress_event(0, 0, "没有媒体文件需要下载")
            return {"total": 0, "success": 0, "fail": 0, "failed": []}

        logger.info("📥 正在下载 %d 个媒体文件...", total_media)

        # 收集所有下载任务 (url, dest, post_bid)
        tasks: list[tuple[str, Path, str]] = []
        comment_destinations: dict[str, dict] = {}
        comment_identity_urls: dict[tuple[str, str], str] = {}
        comment_path_reservations: dict[str, tuple[str, str, str]] = {}
        for post in all_posts:
            for idx, media in enumerate(post.media):
                # 主文件
                fname = self._build_media_filename(post, media, idx)
                dest = self.media_dir / fname
                download_url = media.url

                # 如果是图片，按清晰度转换 URL
                if media.type == MediaType.IMAGE:
                    download_url = transform_image_url(media.url, self.image_quality)

                tasks.append((download_url, dest, post.bid))

                # 实况照片：额外下载静态图片部分
                if media.type == MediaType.LIVE_PHOTO and media.thumbnail:
                    thumb_ext = get_extension(media.thumbnail, MediaType.IMAGE)
                    thumb_fname = f"{post.bid}_live_{idx + 1:02d}.{thumb_ext}"
                    thumb_dest = self.media_dir / thumb_fname
                    tasks.append((media.thumbnail, thumb_dest, post.bid))

                # 视频：下载封面图
                if media.type == MediaType.VIDEO and media.thumbnail:
                    cover_fname = f"{post.bid}_video_{idx + 1:02d}_cover.jpg"
                    cover_dest = self.media_dir / cover_fname
                    tasks.append((media.thumbnail, cover_dest, post.bid))

            if post.link_card and post.link_card.image_url:
                link_ext = get_extension(post.link_card.image_url, MediaType.IMAGE)
                link_dest = self.media_dir / f"{post.bid}_link.{link_ext}"
                tasks.append((post.link_card.image_url, link_dest, post.bid))

            for comment in self._iter_comments(post.comments):
                if not comment.image_url:
                    continue
                comment_dest, expected_extension = self._comment_image_destination(
                    post.bid, comment
                )
                identity_key = (post.bid, comment.id)
                known_url = comment_identity_urls.get(identity_key)
                if known_url is not None and known_url != comment.image_url:
                    raise WeiboError(
                        "同一评论 ID 的图片 URL 冲突",
                        kind=WeiboErrorKind.PARSE,
                        recoverable=False,
                    )
                comment_identity_urls[identity_key] = comment.image_url
                owner = (post.bid, comment.id, comment.image_url)
                reserved_extensions = (
                    {expected_extension}
                    if expected_extension is not None
                    else _DYNAMIC_IMAGE_EXTENSIONS
                )
                reserved_base = f"{post.bid}_{comment.id}"
                for extension in reserved_extensions:
                    reserved_path = (
                        Path("media")
                        / "comments"
                        / f"{reserved_base}.{extension}"
                    ).as_posix()
                    reserved_owner = comment_path_reservations.get(reserved_path)
                    if reserved_owner is not None and reserved_owner != owner:
                        raise WeiboError(
                            "评论图片潜在最终路径冲突",
                            kind=WeiboErrorKind.PARSE,
                            recoverable=False,
                        )
                    comment_path_reservations[reserved_path] = owner
                tasks.append((comment.image_url, comment_dest, post.bid))
                destination_key = str(comment_dest)
                entry = comment_destinations.get(destination_key)
                if entry is None:
                    comment_destinations[destination_key] = {
                        "url": comment.image_url,
                        "expected_extension": expected_extension,
                        "references": [comment],
                    }
                else:
                    if entry["url"] != comment.image_url:
                        raise WeiboError(
                            "同一评论图片目标的 URL 冲突",
                            kind=WeiboErrorKind.PARSE,
                            recoverable=False,
                        )
                    entry["references"].append(comment)

        if comment_destinations:
            probe_fd = self._open_secure_comment_directory()
            _close_directory_reference(probe_fd)
        else:
            self.media_dir.mkdir(parents=True, exist_ok=True)

        # 去重
        seen_dests = set()
        unique_tasks = []
        for url, dest, bid in tasks:
            key = str(dest)
            if key not in seen_dests:
                seen_dests.add(key)
                unique_tasks.append((url, dest, bid))

        if self._uses_pacing():
            self.pacing_scheduler.add_media_requests(
                self._count_pending_downloads(unique_tasks, comment_destinations)
            )

        # 多线程下载
        success = 0
        fail = 0
        failed: list[dict[str, str]] = []
        successful_destinations: set[str] = set()
        resolved_comment_paths: dict[str, str] = {}

        # P1 v1.1.1：动态线程数（小任务用少线程）
        actual_workers = min(self.max_workers, max(1, len(unique_tasks)))
        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            with httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(120.0),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                future_map = {}
                for url, dest, bid in unique_tasks:
                    self._check_cancelled()
                    destination_key = str(dest)
                    if destination_key in comment_destinations:
                        comment_entry = comment_destinations[destination_key]
                        directory_fd = self._open_secure_comment_directory()
                        try:
                            expected_extension = comment_entry["expected_extension"]
                            if (
                                expected_extension is not None
                                and self._inspect_comment_target(directory_fd, dest.name)
                            ):
                                successful_destinations.add(destination_key)
                                resolved_comment_paths[destination_key] = (
                                    Path("media") / "comments" / dest.name
                                ).as_posix()
                                success += 1
                                _close_directory_reference(directory_fd)
                                continue
                            if expected_extension is None:
                                reused_name = self._reuse_extensionless_comment_target(
                                    directory_fd,
                                    dest.name,
                                    comment_entry["references"],
                                )
                                if reused_name is not None:
                                    successful_destinations.add(destination_key)
                                    resolved_comment_paths[destination_key] = (
                                        Path("media") / "comments" / reused_name
                                    ).as_posix()
                                    success += 1
                                    _close_directory_reference(directory_fd)
                                    continue
                            future = executor.submit(
                                self._download_comment_with_owned_fd,
                                client,
                                url,
                                dest,
                                directory_fd,
                                expected_extension,
                            )
                        except Exception:
                            _close_directory_reference(directory_fd)
                            raise
                        future_map[future] = (url, dest, bid)
                        continue
                    if dest.exists() and dest.stat().st_size > 0:
                        success += 1
                        successful_destinations.add(destination_key)
                        continue
                    if dest.exists():
                        dest.unlink()
                    future = executor.submit(
                        self._download_media_file,
                        client,
                        url,
                        dest,
                    )
                    future_map[future] = (url, dest, bid)

                self._emit_progress_event(success + fail, len(unique_tasks), f"媒体 {success + fail}/{len(unique_tasks)}")

                with _media_progress(len(future_map)) as pbar:
                    for future in as_completed(future_map):
                        self._check_cancelled()
                        url, dest, bid = future_map[future]
                        try:
                            outcome = future.result()
                        except (WeiboError, OperationPaused, OperationCancelled):
                            raise
                        except Exception:
                            outcome = None
                        destination_key = str(dest)
                        if destination_key in comment_destinations:
                            ok = isinstance(outcome, str) and bool(outcome)
                            if ok:
                                resolved_comment_paths[destination_key] = (
                                    Path("media") / "comments" / outcome
                                ).as_posix()
                        else:
                            ok = bool(outcome)
                        if ok:
                            success += 1
                            successful_destinations.add(str(dest))
                        else:
                            fail += 1
                            failed.append({
                                "url": url,
                                "dest": str(dest),
                                "bid": bid,
                            })
                        self._emit_progress_event(success + fail, len(unique_tasks), f"媒体 {success + fail}/{len(unique_tasks)}")
                        pbar.update(1)

        # 更新 Post 中的 local_path
        for post in all_posts:
            for idx, media in enumerate(post.media):
                fname = self._build_media_filename(post, media, idx)
                path = self.media_dir / fname
                if path.exists():
                    media.local_path = str(path)

                # 实况/视频的额外文件
                if media.type == MediaType.LIVE_PHOTO and media.thumbnail:
                    thumb_ext = get_extension(media.thumbnail, MediaType.IMAGE)
                    thumb_path = self.media_dir / f"{post.bid}_live_{idx + 1:02d}.{thumb_ext}"
                    if thumb_path.exists():
                        media.local_thumb = str(thumb_path)

                if media.type == MediaType.VIDEO and media.thumbnail:
                    cover_path = self.media_dir / f"{post.bid}_video_{idx + 1:02d}_cover.jpg"
                    if cover_path.exists():
                        media.video_cover = str(cover_path)

            if post.link_card and post.link_card.image_url:
                link_ext = get_extension(post.link_card.image_url, MediaType.IMAGE)
                link_path = self.media_dir / f"{post.bid}_link.{link_ext}"
                if link_path.exists():
                    post.link_card.local_image = str(link_path)

        for destination_key, entry in comment_destinations.items():
            if destination_key not in successful_destinations:
                continue
            relative_path = resolved_comment_paths[destination_key]
            for comment in entry["references"]:
                comment.local_image = relative_path

        logger.info("✅ 下载完成: %d 成功, %d 失败", success, fail)
        return {
            "total": len(unique_tasks),
            "success": success,
            "fail": fail,
            "failed": failed,
        }
