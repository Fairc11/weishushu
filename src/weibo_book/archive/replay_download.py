"""直播分段的有界下载与可验证恢复缓存；不写档案数据库。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from weibo_book.errors import OperationCancelled, OperationPaused, WeiboError, WeiboErrorKind
from .live_replay import MAX_REPLAY_SEGMENTS, parse_m3u8_segments, resolve_segment_url


def checked_directory(path: Path) -> None:
    """只在已经存在的普通父目录内建立一级目录。"""
    for parent in (path.parent, path):
        if parent == path:
            parent.mkdir(exist_ok=True)
        marker = parent.lstat()
        if not stat.S_ISDIR(marker.st_mode) or stat.S_ISLNK(marker.st_mode):
            raise WeiboError("直播恢复缓存目录不安全", kind=WeiboErrorKind.API)


def file_proof(path: Path, check_control=lambda: None) -> dict:
    marker = path.lstat()
    if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
        raise WeiboError("直播缓存必须是单链接普通文件", kind=WeiboErrorKind.API)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    try:
        before = os.fstat(fd)
        identity = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_nlink')
        if any(getattr(marker, k) != getattr(before, k) for k in identity):
            raise WeiboError("直播缓存文件在打开时已变化", kind=WeiboErrorKind.API)
        digest = hashlib.sha256()
        while True:
            check_control()
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        if any(getattr(before, k) != getattr(after, k) for k in identity):
            raise WeiboError("直播缓存文件在校验时已变化", kind=WeiboErrorKind.API)
        return {'size': before.st_size, 'sha256': digest.hexdigest()}
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict) -> None:
    checked_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.proof-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class SegmentBatch:
    directory: Path
    names: tuple[str, ...]
    urls: tuple[str, ...]
    files: tuple[Path, ...]


def download_replay_segments(
    cache_root: Path, playlist_url: str, text: str, names: list[str], *,
    client, download, check_control, progress=None, scheduler=None, max_workers=4, timings=None,
) -> SegmentBatch:
    """并发只限当前回放，返回顺序固定；失败保留通过证明校验的分段。"""
    segments = parse_m3u8_segments(text)
    if (not segments or len(segments) > MAX_REPLAY_SEGMENTS or len(names) != len(segments)
            or len(set(names)) != len(names)
            or any(not re.fullmatch(r'[A-Za-z0-9_-]+\.ts', name) for name in names)):
        raise WeiboError("直播回放分段清单无效", kind=WeiboErrorKind.PARSE)
    if type(max_workers) is not int or not 1 <= max_workers <= 4:
        raise ValueError('直播下载并发数必须为 1 至 4')
    urls = tuple(resolve_segment_url(playlist_url, segment) for segment in segments)
    # URL/清单精确匹配，不推断签名失效前后的内容身份。
    key = hashlib.sha256(json.dumps([playlist_url, text, names], ensure_ascii=True).encode()).hexdigest()
    cache_root = Path(cache_root)
    checked_directory(cache_root)
    directory = cache_root / key
    checked_directory(directory)
    files = tuple(directory / name for name in names)
    completed = 0
    completed_bytes = 0
    pending = []
    stop = threading.Event()
    low = scheduler is not None and scheduler.is_low_intensity
    workers = 1 if low else max_workers

    def notify():
        if progress is not None:
            progress({'current': completed, 'total': len(files), 'unit': 'segment',
                      'completed_bytes': completed_bytes})

    notify()
    for index, path in enumerate(files):
        check_control()
        proof_path = path.with_suffix('.json')
        valid = False
        if path.exists() or path.is_symlink():
            actual = file_proof(path, check_control)
            if proof_path.exists() or proof_path.is_symlink():
                file_proof(proof_path, check_control)
                try:
                    valid = actual['size'] > 0 and json.loads(proof_path.read_text()) == actual
                except (ValueError, UnicodeError):
                    valid = False
            if valid:
                completed += 1
                completed_bytes += actual['size']
                notify()
            else:
                path.unlink()
        if not valid:
            if proof_path.exists() or proof_path.is_symlink():
                file_proof(proof_path, check_control)
                proof_path.unlink()
            pending.append(index)
    if low:
        scheduler.add_media_requests(len(pending))

    class Control:
        def is_set(self):
            check_control()
            return stop.is_set()

    def fetch(index):
        check_control()
        if stop.is_set():
            raise OperationCancelled('直播分段下载已停止')
        path = files[index]
        def operation():
            ok = download(client, urls[index], path, cancel_event=Control(),
                          max_retries=0 if low else 2, raise_request_errors=True)
            if not ok:
                raise WeiboError('直播回放分段下载失败，已保留完成的分段', kind=WeiboErrorKind.NETWORK)
            return ok
        try:
            if low:
                scheduler.run('media', operation)
            else:
                with timings.measure('request') if timings is not None else nullcontext():
                    operation()
        except WeiboError as exc:
            if exc.kind in {WeiboErrorKind.AUTH, WeiboErrorKind.RATE_LIMIT}:
                raise OperationPaused('直播下载需要处理登录或限流状态，任务已暂停',
                                      pause_reason='rate_limited' if exc.kind is WeiboErrorKind.RATE_LIMIT
                                      else 'authentication_required') from exc
            raise
        proof = file_proof(path, check_control)
        if proof['size'] <= 0:
            raise WeiboError('直播回放分段为空', kind=WeiboErrorKind.NETWORK)
        _atomic_json(path.with_suffix('.json'), proof)
        return proof['size']

    # 调度器仅在低强度串行分支使用，避免多线程修改请求预算。
    if workers == 1:
        for index in pending:
            completed_bytes += fetch(index)
            completed += 1
            notify()
    else:
        iterator = iter(pending)
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='weishushu-replay')
        futures = set()
        try:
            for _ in range(min(workers, len(pending))):
                futures.add(executor.submit(fetch, next(iterator)))
            while futures:
                check_control()
                done, _ = wait(futures, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.remove(future)
                    completed_bytes += future.result()
                    completed += 1
                    notify()
                    index = next(iterator, None)
                    if index is not None:
                        futures.add(executor.submit(fetch, index))
        finally:
            stop.set()
            executor.shutdown(wait=True, cancel_futures=True)
    return SegmentBatch(directory, tuple(names), urls, files)


def copy_verified_file(source: Path, destination: Path, check_control) -> None:
    """复制并复核内容，保留恢复源直至整批发布成功。"""
    expected = file_proof(source, check_control)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    destination_fd = None
    try:
        marker = os.fstat(source_fd)
        if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
            raise WeiboError('直播恢复源文件不安全', kind=WeiboErrorKind.API)
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
        digest = hashlib.sha256()
        size = 0
        while True:
            check_control()
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
            view = memoryview(block)
            while view:
                view = view[os.write(destination_fd, view):]
        if {'size':size, 'sha256':digest.hexdigest()} != expected:
            raise WeiboError('直播恢复源文件在复制时发生变化', kind=WeiboErrorKind.API)
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
