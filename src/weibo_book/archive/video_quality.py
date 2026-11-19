"""按视频内容保存一次性核验结果；只执行用户明确选择的旧视频升级。"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from weibo_book.errors import OperationCancelled, OperationPaused
from .media_layout import media_year_month
from .repository import ArchiveError
from .video_playback import video_quality_score

if TYPE_CHECKING:
    from .repository import ArchiveRepository
    from .schema import MediaRecord

logger = logging.getLogger(__name__)
_META = "video_quality_audit"
_SELECTION = "video_quality_selection"


def valid_limit(value: object) -> bool:
    return (type(value) is int and value in {0, 10, 50}) or value == "all"


def _key(record: MediaRecord) -> str:
    return f"{record.owner_id}:{record.position}"


def _fingerprint(record: MediaRecord) -> str:
    # 签名查询串变化不代表文件变化，不能用完整远程 URL 作为核验缓存键。
    return f"sha256:{record.sha256}" if record.sha256 else f"path:{record.local_path}"


def _ledger(repository: ArchiveRepository) -> dict:
    value = repository._read_meta(_META)
    if value is None:
        return {"version": 1, "entries": {}}
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or not isinstance(value.get("entries"), dict):
        raise ArchiveError("视频画质核验记录无效")
    for entry in value["entries"].values():
        if not isinstance(entry, dict) or entry.get("state") not in {"verified", "available", "retry"} or not isinstance(entry.get("fingerprint"), str):
            raise ArchiveError("视频画质核验条目无效")
    return value


def _records(repository: ArchiveRepository) -> dict[str, MediaRecord]:
    posts = {post.bid: post for post in repository.list_posts_for_render() if post.visibility == "visible"}
    by_post: dict[str, list] = {}
    for record in repository.list_media_for_render():
        if record.owner_type == "post" and record.role == "video" and record.owner_id in posts:
            by_post.setdefault(record.owner_id, []).append(record)
    selected = []
    for bid, records in by_post.items():
        if sum(item.get("type") == "video" for item in posts[bid].media_signature) > 1:
            continue  # PC 单视频播放列表不能用于替换多视频微博。
        selected.append(max(records, key=lambda item: video_quality_score(item.remote_url)))
    selected.sort(key=lambda item: item.owner_id)
    selected.sort(key=lambda item: posts[item.owner_id].created_at, reverse=True)
    return {_key(record): record for record in selected}


def _entry(ledger: dict, record: MediaRecord) -> dict | None:
    entry = ledger["entries"].get(_key(record))
    return entry if entry and entry["fingerprint"] == _fingerprint(record) else None


def _remember(repository: ArchiveRepository, record: MediaRecord, state: str) -> None:
    ledger = _ledger(repository)
    ledger["entries"][_key(record)] = {"fingerprint": _fingerprint(record), "state": state}
    repository._write_meta(_META, ledger)


def remember_verified(repository: ArchiveRepository, record: MediaRecord) -> None:
    """下载来源已成功核验时登记；平台最高只有低清也属于已核验。"""
    _remember(repository, record, "verified")


def summary(repository: ArchiveRepository) -> dict[str, int]:
    records = _records(repository)
    ledger = _ledger(repository)
    previous = repository._read_meta("video_quality_checked_v2")
    previous = previous if isinstance(previous, dict) else {}
    result = {"unchecked_count": 0, "available_count": 0, "failed_count": 0}
    for record in records.values():
        entry = _entry(ledger, record)
        # 接续已成功完成 PC 核验的旧记录；更早的 video_ld_confirmed 不可信。
        if entry is None and previous.get(record.owner_id) == record.remote_url:
            remember_verified(repository, record)
            continue
        state = entry["state"] if entry else None
        if state is None:
            result["unchecked_count"] += 1
        elif state == "available":
            result["available_count"] += 1
        elif state == "retry":
            result["failed_count"] += 1
    return result


def scan(repository: ArchiveRepository, source, *, cancel_requested: Callable[[], bool] = lambda: False, progress: Callable[[int, int], None] = lambda _i, _n: None) -> dict[str, int]:
    """只查没有核验结果或此前检查失败的条目，不下载媒体。"""
    summary(repository)
    ledger = _ledger(repository)
    pending = [record for record in _records(repository).values()
               if (entry := _entry(ledger, record)) is None or entry["state"] == "retry"]
    for index, record in enumerate(pending, 1):
        if cancel_requested():
            raise OperationCancelled("已取消旧视频画质检查")
        try:
            best = source.fetch_video_playback_url(record.owner_id)
            if cancel_requested():
                raise OperationCancelled("已取消旧视频画质检查")
            state = "retry" if not best else (
                "available" if video_quality_score(best) > video_quality_score(record.remote_url) else "verified"
            )
            _remember(repository, record, state)
        except (OperationCancelled, OperationPaused):
            raise
        except Exception as exc:
            logger.warning("检查微博 %s 视频画质失败，留待重试: %s", record.owner_id, exc)
            _remember(repository, record, "retry")
        progress(index, len(pending))
    return summary(repository)


def apply_selected(repository: ArchiveRepository, source, run_id: str, limit: int | str, download: Callable, *, cancel_requested: Callable[[], bool] = lambda: False, progress: Callable[[int, int], None] = lambda _i, _n: None) -> int:
    if not valid_limit(limit):
        raise ArchiveError("旧视频更新数量无效")
    if limit == 0:
        return 0
    ledger = _ledger(repository)
    records = _records(repository)
    selection = repository._read_meta(_SELECTION)
    if selection is not None and (
        not isinstance(selection, dict) or set(selection) != {"run_id", "items"}
        or not isinstance(selection["run_id"], str) or not isinstance(selection["items"], list)
    ):
        raise ArchiveError("旧视频更新恢复范围无效")
    if selection is None or selection["run_id"] != run_id:
        chosen = [{"key": key, "fingerprint": _fingerprint(record)} for key, record in records.items()
                  if (entry := _entry(ledger, record)) and entry["state"] == "available"]
        if limit != "all":
            chosen = chosen[:limit]
        selection = {"run_id": run_id, "items": chosen}
        repository._write_meta(_SELECTION, selection)
    items = selection.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) or set(item) != {"key", "fingerprint"}
            or not isinstance(item["key"], str) or not isinstance(item["fingerprint"], str) for item in items):
        raise ArchiveError("旧视频更新恢复范围无效")
    updated = 0
    for index, item in enumerate(items, 1):
        if cancel_requested():
            raise OperationCancelled("已取消旧视频更新")
        record = records.get(item["key"])
        entry = _entry(_ledger(repository), record) if record else None
        if record is None or _fingerprint(record) != item["fingerprint"] or not entry or entry["state"] == "verified":
            continue
        try:
            best = source.fetch_video_playback_url(record.owner_id)
            if not best:
                _remember(repository, record, "retry")
                continue
            if video_quality_score(best) <= video_quality_score(record.remote_url):
                remember_verified(repository, record)
                continue
            post = repository.get_post(record.owner_id)
            year_month = media_year_month(post.created_at) if post else None
            if year_month is None:
                raise ArchiveError("视频缺少有效发布时间，无法更新")
            local_path, digest = download(repository.paths, record.owner_id, record.position, best, year_month)
            if cancel_requested():
                raise OperationCancelled("已取消旧视频更新")
            replacement = replace(record, remote_url=best, local_path=local_path, sha256=digest)
            with repository.transaction():
                repository.upsert_media(replacement)
                remember_verified(repository, replacement)
            updated += 1
        except (OperationCancelled, OperationPaused):
            raise
        except Exception as exc:
            logger.warning("更新微博 %s 视频失败，保留当前文件和待更新记录: %s", record.owner_id, exc)
        finally:
            progress(index, len(items))
    return updated
