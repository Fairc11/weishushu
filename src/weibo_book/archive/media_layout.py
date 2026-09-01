"""归档媒体文件的年-月目录布局、路径形状校验与旧档案自动迁移。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from weibo_book.errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

MEDIA_LAYOUT_VERSION_KEY = "media_layout_version"
# 2 = media 表与文件迁入年-月目录；3 = 同步改写 posts/revisions/comments
# 内嵌 JSON 里的旧平铺媒体路径（媒体表迁移时漏改，会导致 PDF 本地图片加载失败）
MEDIA_LAYOUT_VERSION = 3

_YEAR = re.compile(r"\d{4}")
_MONTH = re.compile(r"0[1-9]|1[0-2]")
_CREATED_AT_YEAR_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])")
_LEGACY_EMBEDDED_REF = re.compile(r"media/(?:posts|comments)/(?!\d{4}/)[^\"\\/]+")
# 存有媒体相对路径的内嵌 JSON 列：(表名, 主键列, JSON 列)
_EMBEDDED_JSON_COLUMNS = (
    ("posts", "bid", "retweeted_json"),
    ("posts", "bid", "link_card_json"),
    ("posts", "bid", "media_signature_json"),
    ("post_revisions", "id", "payload_json"),
    ("comments", "id", "payload_json"),
)


def media_year_month(created_at: object) -> tuple[str, str] | None:
    """从微博发布时间提取 ``(年, 月)``；无法解析时返回 ``None``。"""
    if isinstance(created_at, datetime):
        return f"{created_at.year:04d}", f"{created_at.month:02d}"
    if isinstance(created_at, str):
        match = _CREATED_AT_YEAR_MONTH.match(created_at.strip())
        if match is not None:
            return match.group(1), match.group(2)
    return None


def media_path_shape(value: object) -> tuple[str, ...] | None:
    """校验 ``media/`` 相对路径形状，合法返回分段元组，否则返回 ``None``。

    既有校验语义不变（``media/`` 前缀、POSIX 相对路径、无空段与
    ``.``/``..`` 段）；在此之上只允许再出现 ``{YYYY}/{MM}`` 两层目录
    （四位年 + 两位月，正则锁死），更深的层级一律拒绝。
    """
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    if Path(value).is_absolute():
        return None
    parts = tuple(value.split("/"))
    if (
        len(parts) < 2
        or parts[0] != "media"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    extra = parts[2:-1]
    if not extra:
        return parts
    if (
        len(extra) == 2
        and _YEAR.fullmatch(extra[0]) is not None
        and _MONTH.fullmatch(extra[1]) is not None
    ):
        return parts
    return None


def read_media_layout_version(connection: sqlite3.Connection) -> int:
    """读取媒体布局版本；缺省键视为旧平铺布局 ``1``。"""
    row = connection.execute(
        "SELECT value_json FROM archive_meta WHERE key = ?",
        (MEDIA_LAYOUT_VERSION_KEY,),
    ).fetchone()
    if row is None:
        return 1
    try:
        value = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise WeiboError(
            "归档媒体布局版本记录无效",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
            original=exc,
        ) from exc
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MEDIA_LAYOUT_VERSION:
        raise WeiboError(
            "归档媒体布局版本记录无效",
            kind=WeiboErrorKind.PARSE,
            recoverable=False,
        )
    return value


def write_media_layout_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO archive_meta(key, value_json) VALUES (?, ?)",
        (MEDIA_LAYOUT_VERSION_KEY, json.dumps(version)),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _migration_time_index(
    connection: sqlite3.Connection,
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """建立 微博 bid → (年, 月) 与 评论 id → 所属微博 bid 的索引。"""
    post_year_month: dict[str, tuple[str, str]] = {}
    for bid, created_at in connection.execute("SELECT bid, created_at FROM posts"):
        year_month = media_year_month(created_at)
        if year_month is not None:
            post_year_month[bid] = year_month
    for (payload_json,) in connection.execute(
        "SELECT retweeted_json FROM posts WHERE retweeted_json IS NOT NULL"
    ):
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        bid = payload.get("bid")
        year_month = media_year_month(payload.get("created_at"))
        if isinstance(bid, str) and bid and year_month is not None:
            post_year_month.setdefault(bid, year_month)
    comment_post = dict(
        connection.execute("SELECT id, post_bid FROM comments").fetchall()
    )
    return post_year_month, comment_post


def _embedded_path_index(connection: sqlite3.Connection) -> dict[str, str]:
    """建立 文件名 → 年-月布局路径 索引；重名冲突的文件名剔除不保。"""
    candidates: dict[str, str | None] = {}
    for (local_path,) in connection.execute(
        "SELECT local_path FROM media WHERE local_path LIKE 'media/%/%/%/%'"
    ):
        filename = local_path.rsplit("/", 1)[-1]
        if filename in candidates:
            if candidates[filename] != local_path:
                candidates[filename] = None
        else:
            candidates[filename] = local_path
    return {name: path for name, path in candidates.items() if path is not None}


def _rewrite_embedded_media_paths(
    connection: sqlite3.Connection, stats: dict[str, int]
) -> None:
    """把内嵌 JSON 里的旧平铺媒体路径改写成 media 表记录的年-月路径。

    媒体表迁移只改了 ``media.local_path``，``posts``/``post_revisions``/
    ``comments`` 内嵌 JSON 中的同名片段仍指向旧平铺位置，渲染（尤其 PDF
    打印页）会直接引用它们。按内容寻址文件名等值替换，无法从 media 表
    解析的引用保持原样（本来就是缺失文件，渲染侧已有容错）。
    """
    index = _embedded_path_index(connection)
    for table, id_column, json_column in _EMBEDDED_JSON_COLUMNS:
        rows = connection.execute(
            f"SELECT {id_column}, {json_column} FROM {table} "
            f"WHERE {json_column} LIKE '%media/%'"
        ).fetchall()
        for row_id, text in rows:
            refs = {match.group(0) for match in _LEGACY_EMBEDDED_REF.finditer(text)}
            if not refs:
                continue
            updated = text
            for ref in refs:
                target = index.get(ref.rsplit("/", 1)[-1])
                if target is None:
                    stats["embedded_unresolved"] += 1
                    continue
                updated = updated.replace(ref, target)
            if updated != text:
                connection.execute(
                    f"UPDATE {table} SET {json_column} = ? WHERE {id_column} = ?",
                    (updated, row_id),
                )
                stats["embedded_rows"] += 1


def migrate_media_layout(
    root: Path, connection: sqlite3.Connection
) -> dict[str, int]:
    """旧档案媒体布局自动迁移。

    v1→v2：把 media 表记录的平铺文件搬到年-月目录，逐条提交、可断点恢复；
    已是新布局的记录直接跳过；源文件缺失的记录只更新路径语义（交由清理
    收尾）。v2→v3：改写内嵌 JSON 中的旧平铺媒体路径。全部完成后才把布局
    版本写为最新。校验和不匹配或路径不安全时中止迁移并保持旧布局可用。
    """
    version = read_media_layout_version(connection)
    stats = {"moved": 0, "missing": 0, "skipped": 0, "embedded_rows": 0,
             "embedded_unresolved": 0}
    if version >= MEDIA_LAYOUT_VERSION:
        return stats
    root = Path(root)
    if version < 2:
        _migrate_flat_files(root, connection, stats)
    _rewrite_embedded_media_paths(connection, stats)
    write_media_layout_version(connection, MEDIA_LAYOUT_VERSION)
    if stats["moved"] or stats["missing"]:
        logger.info(
            "归档媒体已迁移到年-月目录：移动 %d 个，源文件缺失 %d 条，保留原路径 %d 条",
            stats["moved"], stats["missing"], stats["skipped"],
        )
    if stats["embedded_rows"] or stats["embedded_unresolved"]:
        logger.info(
            "归档内嵌 JSON 媒体路径已更新：改写 %d 行，无法解析保留 %d 处",
            stats["embedded_rows"], stats["embedded_unresolved"],
        )
    return stats


def _migrate_flat_files(
    root: Path, connection: sqlite3.Connection, stats: dict[str, int]
) -> None:
    """v1→v2：把 media 表记录的平铺文件搬到年-月目录。"""
    post_year_month, comment_post = _migration_time_index(connection)
    rows = connection.execute(
        "SELECT id, owner_id, local_path, sha256 FROM media ORDER BY rowid"
    ).fetchall()
    for row_id, owner_id, local_path, sha256 in rows:
        parts = media_path_shape(local_path)
        if parts is None:
            raise WeiboError(
                "归档媒体路径不安全，已停止迁移",
                kind=WeiboErrorKind.API,
                recoverable=False,
            )
        if len(parts) != 3 or parts[1] not in ("posts", "comments"):
            # 已是新布局、头像平铺或其他无需搬迁的形状
            continue
        if parts[1] == "posts":
            year_month = post_year_month.get(owner_id)
        else:
            year_month = post_year_month.get(comment_post.get(owner_id, ""))
        if year_month is None:
            stats["skipped"] += 1
            logger.warning(
                "无法确定媒体 %s 所属微博的发布时间，保留原平铺路径", local_path
            )
            continue
        target_relative = f"media/{parts[1]}/{year_month[0]}/{year_month[1]}/{parts[2]}"
        source = root.joinpath(*parts)
        target = root.joinpath(*target_relative.split("/"))
        if source.is_symlink() or (source.exists() and not source.is_file()):
            raise WeiboError(
                "归档媒体源文件类型不安全，已停止迁移",
                kind=WeiboErrorKind.API,
                recoverable=False,
            )
        if source.is_file():
            if sha256 and _sha256(source) != sha256:
                raise WeiboError(
                    f"归档媒体校验和不匹配，已停止迁移：{local_path}",
                    kind=WeiboErrorKind.API,
                    recoverable=False,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or (sha256 and _sha256(target) != sha256)
                ):
                    raise WeiboError(
                        "归档媒体迁移目标冲突，已停止迁移",
                        kind=WeiboErrorKind.API,
                        recoverable=False,
                    )
                source.unlink()
            else:
                os.replace(source, target)
            if sha256 and _sha256(target) != sha256:
                raise WeiboError(
                    "归档媒体迁移后校验和不匹配，已停止迁移",
                    kind=WeiboErrorKind.API,
                    recoverable=False,
                )
            stats["moved"] += 1
        else:
            stats["missing"] += 1
        connection.execute(
            "UPDATE media SET local_path = ? WHERE id = ? AND local_path = ?",
            (target_relative, row_id, local_path),
        )
