"""任务状态/取消。前端用 HTTP 轮询兜底，WS 拿实时进度。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from backend.app.schemas import (
    PersonalArchiveTaskActionResponse,
    RecoveryTaskResponse,
)
from backend.app.services.personal_archive_tasks import personal_archive_tasks
from backend.app.services.following_archive_tasks import following_archive_tasks
from backend.app.services.task_manager import task_manager
from backend.app.services.whoami import whoami
from weibo_book.errors import WeiboError, WeiboErrorKind

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)


@router.get("/recovery", response_model=RecoveryTaskResponse)
async def get_recovery_task() -> RecoveryTaskResponse:
    return RecoveryTaskResponse(task=task_manager.recovery_summary())


def _current_identity() -> dict[str, str]:
    try:
        identity = whoami()
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if not isinstance(identity, dict):
        raise HTTPException(status_code=409, detail="当前登录账号信息无效")
    uid = identity.get("uid")
    screen_name = identity.get("screen_name")
    if not isinstance(uid, str) or not uid.strip() or not isinstance(screen_name, str) or not screen_name.strip():
        raise HTTPException(status_code=409, detail="当前登录账号信息无效")
    return {"uid": uid, "screen_name": screen_name}


def _require_task(task_id: str) -> dict:
    snapshot = task_manager.snapshot(task_id)
    if snapshot is None or snapshot.get("task_kind") not in {
        "personal_archive", "following_archive"
    }:
        raise HTTPException(status_code=404, detail="未找到持久任务")
    return snapshot


def _service(snapshot: dict):
    return (
        following_archive_tasks
        if snapshot["task_kind"] == "following_archive"
        else personal_archive_tasks
    )


@router.post("/{task_id}/pause", response_model=PersonalArchiveTaskActionResponse)
async def pause_task(task_id: str) -> PersonalArchiveTaskActionResponse:
    snapshot = _require_task(task_id)
    if not await _service(snapshot).pause(task_id):
        raise HTTPException(status_code=409, detail="当前任务状态不允许暂停")
    return PersonalArchiveTaskActionResponse(task_id=task_id, state="pausing")


@router.post("/{task_id}/resume", response_model=PersonalArchiveTaskActionResponse)
async def resume_task(task_id: str) -> PersonalArchiveTaskActionResponse:
    snapshot = _require_task(task_id)
    try:
        await _service(snapshot).resume(task_id, _current_identity())
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return PersonalArchiveTaskActionResponse(task_id=task_id, state="running")


def _persistent_identity(task_id: str) -> dict[str, str]:
    """登录态失效时的放弃兜底身份：以持久任务记录的目标账号为准。

    放弃只清理本地文件，不访问微博接口，不需要实时 whoami；
    否则 Cookie 过期会让"放弃"永远 401，僵尸任务再也清不掉。
    """
    record = task_manager.get(task_id)
    persistent = record.persistent_record if record is not None else None
    uid = getattr(persistent, "expected_uid", None)
    if not isinstance(uid, str) or not uid.strip():
        raise HTTPException(
            status_code=409,
            detail="持久任务缺少可信账号标识，请先登录后再放弃",
        )
    label = getattr(persistent, "target_label", None)
    screen_name = label if isinstance(label, str) and label.strip() else uid
    return {"uid": uid, "screen_name": screen_name}


@router.post("/{task_id}/abandon", response_model=PersonalArchiveTaskActionResponse)
async def abandon_task(task_id: str) -> PersonalArchiveTaskActionResponse:
    snapshot = _require_task(task_id)
    try:
        identity = _current_identity()
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        identity = _persistent_identity(task_id)
    try:
        abandoned = await _service(snapshot).abandon(task_id, identity)
    except WeiboError as exc:
        status = 401 if exc.kind == WeiboErrorKind.AUTH else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("放弃未完成部分失败: task_id=%s", task_id)
        raise HTTPException(
            status_code=500,
            detail="放弃未完成部分失败，请查看日志",
        ) from exc
    if not abandoned:
        raise HTTPException(status_code=409, detail="当前任务状态不允许放弃")
    return PersonalArchiveTaskActionResponse(task_id=task_id, state="abandoned")


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict:
    snap = task_manager.snapshot(task_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="未找到任务")
    return snap


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    snap = task_manager.snapshot(task_id)
    ok = await _service(snap).cancel(task_id) if snap is not None and snap.get(
        "task_kind"
    ) in {"personal_archive", "following_archive"} else await task_manager.cancel(task_id)
    if not ok:
        snap = task_manager.snapshot(task_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="未找到任务")
        return {"cancelled": False, "state": snap["state"]}
    return {"cancelled": True}
