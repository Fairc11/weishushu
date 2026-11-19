"""归档根目录布局版本、固定路径解析与根布局 v1→v2 迁移。

布局 v1（旧）：根下 ``manifest.json``、``data/``、``media/``。
布局 v2（新）：书文件留在根下，数据与媒体集中到 ``数据与媒体/``::

    <昵称_UID>/
    ├── 微博书.html / 微博书.pdf / 微博书.md
    └── 数据与媒体/
        ├── 数据/    archive.db(+wal/shm)、archive-data.js、manifest.json、渲染临时文件
        └── 媒体/    微博图片/ 评论图片/ 头像/ 封面/

v2 起 ``media.local_path`` 相对 ``数据与媒体/`` 目录（形如
``媒体/微博图片/2026/08/x.jpg``）；v1 相对归档根（形如
``media/posts/2026/08/x.jpg``）。

macOS 的 APFS/HFS+ 可能把中文目录名存成 NFD：所有「期望中文名 vs 磁盘
实际名」的比较一律先 ``unicodedata.normalize("NFC", ...)``；写入一律
使用本模块的 NFC 常量。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from weibo_book.errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

ROOT_LAYOUT_VERSION = 2
ROOT_LAYOUT_VERSION_KEY = "root_layout_version"

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "archive.db"


def normalize_nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


ASSETS_DIR_NAME = normalize_nfc("数据与媒体")
DATA_DIR_NAME = normalize_nfc("数据")
MEDIA_ROOT_NAME = normalize_nfc("媒体")
LEGACY_DATA_DIR_NAME = "data"
LEGACY_MEDIA_ROOT_NAME = "media"

BOOK_HTML_NAME = normalize_nfc("微博书.html")
BOOK_PDF_NAME = normalize_nfc("微博书.pdf")
BOOK_MARKDOWN_NAME = normalize_nfc("微博书.md")
ARCHIVE_DATA_NAME = "archive-data.js"
RENDER_JOURNAL_NAME = ".weishushu-render-state.json"
RENDER_LOCK_NAME = ".weishushu-render.lock"

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def find_child_nfc(directory: Path, nfc_name: str) -> Path | None:
    """在 ``directory`` 里找 NFC 规范化后等于 ``nfc_name`` 的直接子项。"""
    try:
        for entry in directory.iterdir():
            if normalize_nfc(entry.name) == nfc_name:
                return entry
    except OSError:
        return None
    return None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | _O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ArchivePaths:
    """按根布局版本解析归档内的固定路径。"""

    root: Path
    version: int

    def __post_init__(self) -> None:
        if self.version not in (1, ROOT_LAYOUT_VERSION):
            raise WeiboError("归档根布局版本不受支持", kind=WeiboErrorKind.PARSE)

    @property
    def assets_dir(self) -> Path:
        """``media.local_path`` 的解析基准目录。"""
        if self.version >= 2:
            return self.root / ASSETS_DIR_NAME
        return self.root

    @property
    def data_dir(self) -> Path:
        if self.version >= 2:
            return self.assets_dir / DATA_DIR_NAME
        return self.root / LEGACY_DATA_DIR_NAME

    @property
    def media_root(self) -> Path:
        if self.version >= 2:
            return self.assets_dir / MEDIA_ROOT_NAME
        return self.root / LEGACY_MEDIA_ROOT_NAME

    @property
    def manifest_path(self) -> Path:
        if self.version >= 2:
            return self.data_dir / MANIFEST_NAME
        return self.root / MANIFEST_NAME

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_NAME

    @property
    def archive_data_path(self) -> Path:
        return self.data_dir / ARCHIVE_DATA_NAME

    @property
    def render_journal_path(self) -> Path:
        return self.data_dir / RENDER_JOURNAL_NAME

    @property
    def render_lock_path(self) -> Path:
        return self.data_dir / RENDER_LOCK_NAME

    @property
    def html_path(self) -> Path:
        return self.root / BOOK_HTML_NAME

    @property
    def pdf_path(self) -> Path:
        return self.root / BOOK_PDF_NAME

    @property
    def markdown_path(self) -> Path:
        return self.root / BOOK_MARKDOWN_NAME

    @property
    def data_script_src(self) -> str:
        """书文件（位于归档根）引用 ``archive-data.js`` 的相对地址。"""
        if self.version >= 2:
            return f"{ASSETS_DIR_NAME}/{DATA_DIR_NAME}/{ARCHIVE_DATA_NAME}"
        return f"{LEGACY_DATA_DIR_NAME}/{ARCHIVE_DATA_NAME}"

    @property
    def browser_url_prefix(self) -> str:
        """渲染快照里 ``browser_url`` 相对归档根的前缀（末尾带 ``/``）。"""
        if self.version >= 2:
            return f"{ASSETS_DIR_NAME}/"
        return ""

    def media_file_path(self, local_path: str) -> Path:
        """把 ``media.local_path`` 解析为文件系统路径（调用方先校验形状）。"""
        return self.assets_dir.joinpath(*local_path.split("/"))


def detect_root_layout(root: Path) -> int:
    """按文件系统识别根布局版本；找不到任何清单位置时按 v1 处理。

    只读，不迁移；新清单位置（``数据与媒体/数据/manifest.json``）优先。
    """
    root = Path(root)
    assets = find_child_nfc(root, ASSETS_DIR_NAME)
    if assets is not None and assets.is_dir() and not assets.is_symlink():
        data = find_child_nfc(assets, DATA_DIR_NAME)
        if data is not None and data.is_dir() and not data.is_symlink():
            if find_child_nfc(data, MANIFEST_NAME) is not None:
                return ROOT_LAYOUT_VERSION
    return 1


def read_root_layout_version(connection: sqlite3.Connection) -> int:
    """读取根布局版本记录；缺省视为旧布局 ``1``。"""
    row = connection.execute(
        "SELECT value_json FROM archive_meta WHERE key = ?",
        (ROOT_LAYOUT_VERSION_KEY,),
    ).fetchone()
    if row is None:
        return 1
    try:
        value = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise WeiboError(
            "归档根布局版本记录无效",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
            original=exc,
        ) from exc
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= ROOT_LAYOUT_VERSION
    ):
        raise WeiboError(
            "归档根布局版本记录无效",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    return value


def write_root_layout_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO archive_meta(key, value_json) VALUES (?, ?)",
        (ROOT_LAYOUT_VERSION_KEY, json.dumps(version)),
    )


def _ensure_real_directory(path: Path, message: str) -> None:
    marker = path.lstat()
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
        raise WeiboError(message, kind=WeiboErrorKind.API)


def _canonical_child(directory: Path, nfc_name: str) -> Path:
    """返回 ``directory`` 下 NFC 等于 ``nfc_name`` 的子目录，并把磁盘名规范为 NFC。"""
    found = find_child_nfc(directory, nfc_name)
    if found is None:
        return directory / nfc_name
    if found.name != nfc_name and found.is_dir() and not found.is_symlink():
        canonical = directory / nfc_name
        os.replace(found, canonical)
        _fsync_directory(directory)
        return canonical
    return found


def _move_entry(source: Path, target: Path) -> None:
    marker = source.lstat()
    if stat.S_ISLNK(marker.st_mode):
        raise WeiboError("归档目录迁移源不能是符号链接", kind=WeiboErrorKind.API)
    if target.exists() or target.is_symlink():
        raise WeiboError("归档目录迁移目标冲突，已停止迁移", kind=WeiboErrorKind.API)
    os.replace(source, target)
    _fsync_directory(target.parent)


def _move_directory(source: Path, target: Path) -> None:
    """把 ``source`` 目录整体搬到 ``target``；目标已存在时逐条目并入。"""
    _ensure_real_directory(source, "归档目录迁移源类型不安全")
    if not target.exists() and not target.is_symlink():
        os.replace(source, target)
        _fsync_directory(target.parent)
        return
    _ensure_real_directory(target, "归档目录迁移目标类型不安全")
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        _move_entry(entry, target / entry.name)
    source.rmdir()
    _fsync_directory(source.parent)


def migrate_root_layout(root: Path) -> bool:
    """把根布局 v1 归档整体搬到 v2；幂等、可断点恢复。

    返回是否发生了文件搬动。只做目录/文件搬移，不改数据库内容；媒体
    子目录改名与 ``local_path`` 改写由媒体布局迁移（3→4）完成。
    不是归档根（新旧清单、新旧数据目录都不存在）时不做任何事，交由
    后续标记检查报错。
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return False
    legacy_manifest = find_child_nfc(root, MANIFEST_NAME)
    legacy_data = find_child_nfc(root, LEGACY_DATA_DIR_NAME)
    legacy_media = find_child_nfc(root, LEGACY_MEDIA_ROOT_NAME)
    assets_existing = find_child_nfc(root, ASSETS_DIR_NAME)
    if (
        legacy_manifest is None
        and legacy_data is None
        and legacy_media is None
    ):
        # 已全部就位（或不是归档根）：没有可搬的内容
        return False
    moved = False
    if assets_existing is None:
        assets = root / ASSETS_DIR_NAME
        assets.mkdir()
        moved = True
    else:
        assets = _canonical_child(root, ASSETS_DIR_NAME)
        _ensure_real_directory(assets, "归档数据与媒体目录类型不安全")
    data_target = _canonical_child(assets, DATA_DIR_NAME)
    media_target = _canonical_child(assets, MEDIA_ROOT_NAME)
    # 顺序：先搬 data/ 与 media/，最后搬清单—— detect_root_layout 以
    # 新清单存在与否判定布局，中断后重进能从未搬完的旧位置继续。
    if legacy_data is not None:
        if legacy_data.is_symlink() or not legacy_data.is_dir():
            raise WeiboError("归档数据目录类型不安全，已停止迁移", kind=WeiboErrorKind.API)
        _move_directory(legacy_data, data_target)
        moved = True
    if legacy_media is not None:
        if legacy_media.is_symlink() or not legacy_media.is_dir():
            raise WeiboError("归档媒体目录类型不安全，已停止迁移", kind=WeiboErrorKind.API)
        _move_directory(legacy_media, media_target)
        moved = True
    if legacy_manifest is not None:
        if legacy_manifest.is_symlink() or not legacy_manifest.is_file():
            raise WeiboError("归档清单类型不安全，已停止迁移", kind=WeiboErrorKind.API)
        manifest_target = data_target / MANIFEST_NAME
        if manifest_target.exists() or manifest_target.is_symlink():
            raise WeiboError("归档清单迁移目标冲突，已停止迁移", kind=WeiboErrorKind.API)
        _move_entry(legacy_manifest, manifest_target)
        moved = True
    if moved:
        _fsync_directory(root)
        logger.info("归档目录已迁移到数据与媒体布局：%s", root.name)
    return moved
