"""v1.2.0 M3-10 拆分：历史记录 + 全局搜索（v1.1.6 特性独立 router）。

从 routers/router_backup.py 抽出 list + search 端点。
复用 services/backup_index._validate_output_dir 路径校验。

端点：
- POST /api/backup/list   扫 <output_dir>/微博书_*.md 列表（mtime 降序 + BID 粗估）
- POST /api/backup/search 搜 md 内容，命中行 + 上下文
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Query

from backend.app.schemas import (
    HistoryEntry,
    HistoryListResponse,
    SearchHit,
    SearchResponse,
)
from backend.app.services.backup_index import _validate_output_dir

logger = logging.getLogger(__name__)

router = APIRouter(tags=["history"])


# ====== v1.1.6-1：历史记录面板 ======

@router.post("/api/backup/list", response_model=HistoryListResponse)
async def api_backup_list(path: str = Query(..., description="output_dir 绝对路径")) -> HistoryListResponse:
    """扫 <output_dir>/微博书_*.md 列表（生成历史）。

    v1.2.0 P0 (B-15)：list/search 是只读端点，不应自动 mkdir——
    攻击者传 `path=C:\\Windows\\System32` 等关键目录，前端拼错路径时
    都会无意义地创建空目录。修法：父目录不存在返 404，删掉 mkdir。
    """
    p = _validate_output_dir(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"目录不存在: {p}")

    entries: list[HistoryEntry] = []
    for f in sorted(p.glob("微博书_*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            stat = f.stat()
            # 粗估 BID 数（grep ^## 或 /bid/ 出现次数）
            text = f.read_text(encoding="utf-8", errors="ignore")
            bids_count = text.count("/status/") + text.count("bid=")
            entries.append(HistoryEntry(
                filename=f.name,
                filepath=str(f.resolve()),
                created_at=stat.st_mtime,
                size_bytes=stat.st_size,
                bids_count=bids_count,
            ))
        except OSError as e:
            logger.warning("读 %s 失败: %s", f, e)
            continue
    return HistoryListResponse(entries=entries, total=len(entries))


# ====== v1.1.6-2：全局搜索 ======

@router.post("/api/backup/search", response_model=SearchResponse)
async def api_backup_search(
    path: str = Query(..., description="output_dir 绝对路径"),
    q: str = Query(..., min_length=1, description="搜索关键词"),
    case_sensitive: bool = Query(False),
) -> SearchResponse:
    """扫 <output_dir>/微博书_*.md 内容，命中行 + 上下文。

    v1.2.0 P0 (B-15)：同上，删掉自动 mkdir。
    """
    p = _validate_output_dir(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"目录不存在: {p}")

    pattern = re.compile(re.escape(q), 0 if case_sensitive else re.IGNORECASE)
    hits: list[SearchHit] = []
    files_scanned = 0

    for f in sorted(p.glob("微博书_*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        files_scanned += 1
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                hits.append(SearchHit(
                    filename=f.name,
                    filepath=str(f.resolve()),
                    line_no=i,
                    line_text=line[:500],  # 防爆
                    context_before=lines[i - 2][:200] if i >= 2 else "",
                    context_after=lines[i][:200] if i < len(lines) else "",
                ))

    return SearchResponse(hits=hits, query=q, files_scanned=files_scanned)
