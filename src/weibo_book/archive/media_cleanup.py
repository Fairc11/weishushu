"""安全清理微博归档中已失去数据库引用的媒体文件。"""

from __future__ import annotations

import stat
from collections.abc import Iterable
from pathlib import Path

from .media_layout import media_path_shape
from .repository import ArchiveError
from .schema import MediaRecord


_MANAGED_DIRECTORIES = (
    "media",
)


def _validated_reference_paths(records: Iterable[MediaRecord]) -> set[str]:
    references: set[str] = set()
    for record in records:
        if media_path_shape(record.local_path) is None:
            raise ArchiveError("数据库媒体路径不安全，已停止清理")
        references.add(record.local_path)
    return references


def cleanup_unreferenced_media(
    archive_root: Path,
    records: Iterable[MediaRecord],
) -> list[str]:
    """删除受管理目录中没有数据库引用的普通文件。

    符号链接文件和非普通文件不会被删除；``media/`` 内任何目录
    （含年-月子目录）若为符号链接，则终止本次清理。
    """
    root = Path(archive_root)
    try:
        root_marker = root.lstat()
        if stat.S_ISLNK(root_marker.st_mode) or not stat.S_ISDIR(root_marker.st_mode):
            raise ArchiveError("归档根目录必须是非链接目录")
        physical_root = root.resolve(strict=True)
        references = _validated_reference_paths(records)
        removed: list[str] = []

        def visit(directory: Path) -> None:
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
                relative = entry.relative_to(root).as_posix()
                if relative in references:
                    continue
                entry.unlink()
                removed.append(relative)

        for relative_directory in _MANAGED_DIRECTORIES:
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
        return sorted(removed)
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveError("清理无引用媒体失败", original=exc) from exc
