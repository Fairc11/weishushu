"""登录：扫码 + Chrome 导入 + 状态查询。

阶段 1 简化：扫码 / Chrome 导入都是耗时操作，先返回 task_id 让前端走 WS 等结果。
chrome_import.import_from_chrome() 是同步的，封到 BackgroundTasks。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from backend.app.features import raise_future_feature
from backend.app.schemas import LoginStatus
from backend.app.services.qrcode_login import qrcode_login_service
from backend.app.services.rate_limit import check_login
from backend.app.services.task_manager import run_in_background, task_manager
from weibo_book import WeiboBook
from weibo_book import login as login_service
from weibo_book.errors import OperationCancelled, WeiboError, WeiboErrorKind
from weibo_book.login import load_cookies

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/login", tags=["login"])

NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _run_qrcode_login() -> dict:
    """执行一次扫码，并且只接受通过微博服务端校验的登录态。"""
    cookies = WeiboBook().ensure_login(force=True)
    if not cookies:
        raise WeiboError("扫码登录失败或超时", kind=WeiboErrorKind.AUTH)
    stored = load_cookies(None)
    if not stored or not login_service.validate_stored_cookies(None):
        raise WeiboError("扫码登录未通过微博校验", kind=WeiboErrorKind.AUTH)
    return stored


def _run_chrome_import() -> dict:
    """同步执行 Chrome 导入；路由层必须经 asyncio.to_thread 调用。"""
    from weibo_book.chrome_import import import_from_chrome

    cookies = import_from_chrome()
    if not cookies:
        raise WeiboError("Chrome 导入失败：未找到 m.weibo.cn cookie（请确认 Chrome 已登录过）", kind=WeiboErrorKind.AUTH)
    return {"logged_in": True, "cookie_source": "chrome", "count": len(cookies)}


@router.get("/status", response_model=LoginStatus)
async def status() -> LoginStatus:
    """当前登录状态：缓存 cookie 存在且仍通过微博校验。

    v1.2.0 P0 (B-10)：原 except 静默吞错，磁盘读失败 / cookie 文件损坏 都不留痕迹，
    排查"为什么状态总显示未登录"很困难。修法：logger.exception + 返 cookie_source=error
    让前端浮动日志能看到堆栈。
    """
    try:
        stored = load_cookies(None)
        if stored:
            valid = await asyncio.to_thread(login_service.validate_stored_cookies, None)
            if valid:
                return LoginStatus(logged_in=True, cookie_source="file")
            return LoginStatus(logged_in=False, cookie_source="expired")
    except Exception as e:
        logger.exception("status check failed: %s", e)
        return LoginStatus(logged_in=False, cookie_source="error")
    return LoginStatus(logged_in=False, cookie_source="none")


@router.post("/logout")
async def logout() -> dict:
    """退出登录：删除当前 profile 的 Cookie 文件。

    只清理本机当前身份的登录态文件；档案、日志、设置和其他 profile 数据不动。
    前端随后重新拉取 /status 刷新显示。
    """
    path = login_service.get_cookie_file_path(None)
    if not path.exists():
        return {"logged_in": False, "cleared": False}
    try:
        await asyncio.to_thread(path.unlink)
    except OSError as e:
        logger.exception("logout failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"退出登录失败：无法删除登录文件（{e}）",
        ) from e
    logger.info("已退出登录并删除当前 profile 的 Cookie 文件")
    return {"logged_in": False, "cleared": True}


@router.post("/qrcode")
async def qrcode(bg: BackgroundTasks, request: Request) -> dict:
    """创建进程级单会话，Playwright 始终在工作线程中运行。"""
    check_login(request)
    try:
        session = qrcode_login_service.reserve()
    except WeiboError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task_id = await task_manager.create()
    qrcode_login_service.bind_task(session.session_id, task_id)

    async def _run() -> dict:
        worker = asyncio.create_task(
            asyncio.to_thread(qrcode_login_service.run, session.session_id)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            qrcode_login_service.cancel(session.session_id)
            try:
                await asyncio.shield(worker)
            except OperationCancelled:
                pass
            raise

    bg_task = asyncio.create_task(run_in_background(task_id, _run))
    rec = task_manager.get(task_id)
    if rec is not None:
        rec._asyncio_task = bg_task
    return {"task_id": task_id, "session_id": session.session_id}


def _qrcode_http_error(exc: WeiboError) -> HTTPException:
    status_code = 404 if exc.kind == WeiboErrorKind.NOT_FOUND else 409
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/qrcode/{session_id}/status")
async def qrcode_status(session_id: str) -> dict:
    try:
        return qrcode_login_service.status(session_id)
    except WeiboError as exc:
        raise _qrcode_http_error(exc) from exc


@router.get("/qrcode/{session_id}/image")
async def qrcode_image(session_id: str) -> Response:
    try:
        image = qrcode_login_service.image(session_id)
    except WeiboError as exc:
        raise _qrcode_http_error(exc) from exc
    if image is None:
        raise HTTPException(
            status_code=409,
            detail="二维码尚未就绪",
            headers=NO_STORE_HEADERS,
        )
    return Response(
        content=image,
        media_type="image/png",
        headers=NO_STORE_HEADERS,
    )


@router.post("/qrcode/{session_id}/cancel")
async def qrcode_cancel(session_id: str) -> dict:
    try:
        accepted = qrcode_login_service.cancel(session_id)
        task_id = qrcode_login_service.task_id(session_id)
    except WeiboError as exc:
        raise _qrcode_http_error(exc) from exc
    if task_id is not None:
        await task_manager.cancel(task_id)
    closed = await asyncio.to_thread(
        qrcode_login_service.wait_closed,
        session_id,
        35,
    )
    if accepted and not closed:
        raise HTTPException(
            status_code=500,
            detail="二维码登录资源未能及时关闭",
        )
    return {"cancelled": accepted, "closed": closed}


@router.post("/chrome")
async def chrome_import(bg: BackgroundTasks, request: Request) -> dict:
    """从 Chrome 导入 cookie（需 Chrome 已登录过 weibo.cn）。

    import_from_chrome() 使用 Playwright Sync API，必须与扫码登录一样经
    asyncio.to_thread 放进默认线程池；直接在事件循环里调用会触发
    "Sync API inside the asyncio loop"。
    """
    raise_future_feature()
    check_login(request)  # S4 v1.1.1
    task_id = await task_manager.create()

    async def _run() -> dict:
        return await asyncio.to_thread(_run_chrome_import)

    bg_task = asyncio.create_task(run_in_background(task_id, _run))
    rec = task_manager.get(task_id)
    if rec is not None:
        rec._asyncio_task = bg_task
    return {"task_id": task_id}
