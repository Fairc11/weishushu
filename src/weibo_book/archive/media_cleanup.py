"""安全清理微博归档中已失去数据库引用的媒体文件。"""

from __future__ import annotations

import stat
from collections.abc import Iterable
from pathlib import Path

from .media_layout import media_path_shape
from .paths import (
    LEGACY_MEDIA_ROOT_NAME,
    MEDIA_ROOT_NAME,
    ArchivePaths,
    normalize_nfc,
)
from .repository import ArchiveError
from .schema import MediaRecord


def _managed_directories(paths: ArchivePaths) -> tuple[str, ...]:
    if paths.version >= 2:
        return (MEDIA_ROOT_NAME,)
    return (LEGACY_MEDIA_ROOT_NAME,)


def _validated_reference_paths(records: Iterable[MediaRecord]) -> set[str]:
    references: set[str] = set()
    for record in records:
        if media_path_shape(record.local_path) is None:
            raise ArchiveError("数据库媒体路径不安全，已停止清理")
        references.add(normalize_nfc(record.local_path))
    return references


def cleanup_unreferenced_media(
    paths: ArchivePaths,
    records: Iterable[MediaRecord],
    extra_references: Iterable[str] = (),
    progress_callback=None,
) -> list[str]:
    """删除受管理目录中没有数据库引用的普通文件。

    ``extra_references`` 是 media 表之外仍受保护的相对路径（如
    profile_assets 记录的头像历史与封面文件）。符号链接文件和非普通
    文件不会被删除；受管理媒体目录内任何目录（含年-月子目录）若为符号
    链接，则终止本次清理。
    """
    root = Path(paths.assets_dir)
    try:
        root_marker = root.lstat()
        if stat.S_ISLNK(root_marker.st_mode) or not stat.S_ISDIR(root_marker.st_mode):
            raise ArchiveError("归档媒体基准目录必须是非链接目录")
        physical_root = root.resolve(strict=True)
        references = _validated_reference_paths(records)
        for value in extra_references:
            if not value:
                continue
            if media_path_shape(value) is None:
                raise ArchiveError("数据库媒体路径不安全，已停止清理")
            references.add(normalize_nfc(value))
        removed: list[str] = []
        visited = 0

        def visit(directory: Path) -> None:
            nonlocal visited
            for entry in sorted(directory.iterdir(), key=lambda item: item.name):
                marker = entry.lstat()
                if stat.S_ISLNK(marker.st_mode):
                    try:
                        target_is_directory = stat.S_ISDIR(entry.stat().st_mode)
                    except OSError:
                        target_is_directory = False
                    if target_is_directory:
                        raise ArchiveError("受管理媒体目录不能是符号链接")
                    continue
                if stat.S_ISDIR(marker.st_mode):
                    if not entry.resolve(strict=True).is_relative_to(physical_root):
                        raise ArchiveError("受管理媒体路径越界，已停止清理")
                    visit(entry)
                    continue
                if not stat.S_ISREG(marker.st_mode):
                    continue
                visited += 1
                if progress_callback is not None and visited % 100 == 0:
                    progress_callback(visited, None)
                relative = normalize_nfc(entry.relative_to(root).as_posix())
                if relative in references:
                    continue
                entry.unlink()
                removed.append(relative)

        for relative_directory in _managed_directories(paths):
            directory = root.joinpath(*relative_directory.split("/"))
            try:
                marker = directory.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(marker.st_mode):
                raise ArchiveError("受管理媒体目录不能是符号链接")
            if not stat.S_ISDIR(marker.st_mode):
                raise ArchiveError("受管理媒体路径必须是目录")
            if not directory.resolve(strict=True).is_relative_to(physical_root):
                raise ArchiveError("受管理媒体路径越界，已停止清理")
            visit(directory)
        if progress_callback is not None:
            progress_callback(visited, visited)
        return sorted(removed)
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveError("清理无引用媒体失败", original=exc) from exc
