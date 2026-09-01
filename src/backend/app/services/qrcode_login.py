"""进程级二维码登录会话。

二维码只保存在内存中；Playwright 页面、context 和 browser 始终由
创建它们的工作线程关闭。
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator

from weibo_book.errors import (
    OperationCancelled,
    WeiboError,
    WeiboErrorKind,
)

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_SECONDS = 120
MAX_QRCODE_PNG_BYTES = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PRIMARY_LOGIN_URL = "https://weibo.com/login"
FALLBACK_LOGIN_URL = "https://passport.weibo.cn/signin/login"
QRCODE_SELECTOR = 'img[src^="https://v2.qr.weibo.cn/"]'
QRCODE_HEADING = "扫描二维码登录"

ACTIVE_STATES = frozenset({"preparing", "waiting_scan", "validating"})
TERMINAL_STATES = frozenset(
    {"authenticated", "expired", "error", "cancelled"}
)


def _cookie_fingerprint(cookies: list[dict]) -> tuple[tuple[str, str, str, str], ...]:
    """只在工作线程内比较 Cookie 变化，不返回给 API 或日志。"""
    return tuple(sorted(
        (
            str(cookie.get("name", "")),
            str(cookie.get("domain", "")),
            str(cookie.get("path", "/")),
            str(cookie.get("value", "")),
        )
        for cookie in cookies
    ))


@dataclass(frozen=True)
class QRCodeLoginEvent:
    kind: str
    payload: object | None = None

    @classmethod
    def qrcode(cls, png: bytes) -> "QRCodeLoginEvent":
        return cls("qrcode", png)

    @classmethod
    def cookies(cls, cookies: list[dict]) -> "QRCodeLoginEvent":
        return cls("cookies", cookies)

    @classmethod
    def tick(cls) -> "QRCodeLoginEvent":
        return cls("tick")


@dataclass
class QRCodeLoginSession:
    session_id: str
    created_at: float
    deadline: float
    task_id: str | None = None
    state: str = "preparing"
    message: str = "正在准备二维码"
    qrcode_png: bytes | None = None
    result: dict | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    closed_event: threading.Event = field(default_factory=threading.Event)


class QRCodeLoginControl:
    def __init__(
        self,
        session: QRCodeLoginSession,
        monotonic: Callable[[], float],
    ) -> None:
        self.session_id = session.session_id
        self._session = session
        self._monotonic = monotonic

    def cancel_requested(self) -> bool:
        return self._session.cancel_event.is_set()

    def remaining_seconds(self) -> int:
        return max(0, math.ceil(self._session.deadline - self._monotonic()))

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested():
            raise OperationCancelled("二维码登录已取消")


Worker = Callable[[QRCodeLoginControl], Iterator[QRCodeLoginEvent]]
Validator = Callable[[list[dict]], bool]
Saver = Callable[[list[dict]], list[dict]]


def _default_validator(cookies: list[dict]) -> bool:
    from weibo_book.login import check_cookies_valid

    return check_cookies_valid(cookies)


def _default_saver(cookies: list[dict]) -> list[dict]:
    from weibo_book.login import save_cookies

    return save_cookies(cookies)


class QRCodeLoginService:
    def __init__(
        self,
        *,
        worker: Worker | None = None,
        validator: Validator = _default_validator,
        saver: Saver = _default_saver,
        timeout_seconds: int = SESSION_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("二维码会话时长必须大于 0 秒")
        self._worker = worker or self._playwright_events
        self._validator = validator
        self._saver = saver
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._session: QRCodeLoginSession | None = None

    def reserve(self) -> QRCodeLoginSession:
        with self._lock:
            current = self._session
            if current is not None and (
                current.state in ACTIVE_STATES or not current.closed_event.is_set()
            ):
                raise WeiboError(
                    "已有二维码登录会话正在运行，请先完成或取消",
                    kind=WeiboErrorKind.API,
                )
            now = self._monotonic()
            session = QRCodeLoginSession(
                session_id=str(uuid.uuid4()),
                created_at=now,
                deadline=now + self._timeout_seconds,
            )
            self._session = session
            return session

    def bind_task(self, session_id: str, task_id: str) -> None:
        with self._lock:
            session = self._require(session_id)
            if session.task_id is not None:
                raise WeiboError(
                    "二维码登录会话已经绑定后台任务",
                    kind=WeiboErrorKind.API,
                )
            session.task_id = task_id

    def status(self, session_id: str) -> dict:
        with self._lock:
            session = self._require(session_id)
            remaining = (
                0
                if session.state in TERMINAL_STATES
                else max(0, math.ceil(session.deadline - self._monotonic()))
            )
            return {
                "session_id": session.session_id,
                "task_id": session.task_id,
                "state": session.state,
                "message": session.message,
                "remaining_seconds": remaining,
                "image_ready": session.qrcode_png is not None,
                "result": dict(session.result) if session.result is not None else None,
            }

    def image(self, session_id: str) -> bytes | None:
        with self._lock:
            image = self._require(session_id).qrcode_png
            return bytes(image) if image is not None else None

    def task_id(self, session_id: str) -> str | None:
        with self._lock:
            return self._require(session_id).task_id

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            session = self._require(session_id)
            if session.state not in ACTIVE_STATES:
                return False
            session.cancel_event.set()
            session.state = "cancelled"
            session.message = "已取消扫码登录"
            session.qrcode_png = None
            session.result = None
            return True

    def wait_closed(self, session_id: str, timeout: float) -> bool:
        with self._lock:
            closed_event = self._require(session_id).closed_event
        return closed_event.wait(timeout)

    def run(self, session_id: str) -> dict:
        with self._lock:
            session = self._require(session_id)
        control = QRCodeLoginControl(session, self._monotonic)
        events = iter(self._worker(control))
        try:
            for event in events:
                self._raise_if_cancelled(session)
                self._raise_if_expired(session)
                if event.kind == "tick":
                    continue
                if event.kind == "qrcode":
                    self._accept_qrcode(session, event.payload)
                    continue
                if event.kind == "cookies":
                    cookies = event.payload
                    if not isinstance(cookies, list):
                        raise WeiboError(
                            "二维码登录返回的 Cookie 结构无效",
                            kind=WeiboErrorKind.PARSE,
                        )
                    self._set_state(session, "validating", "正在校验登录状态")
                    if not self._validator(cookies):
                        continue
                    saved = self._saver(cookies)
                    result = {
                        "logged_in": True,
                        "cookie_source": "qrcode",
                        "count": len(saved),
                    }
                    with self._lock:
                        session.state = "authenticated"
                        session.message = "登录成功"
                        session.qrcode_png = None
                        session.result = result
                    return result
                raise WeiboError(
                    "二维码登录事件类型无效",
                    kind=WeiboErrorKind.PARSE,
                )
            self._raise_if_cancelled(session)
            self._raise_if_expired(session)
            raise WeiboError(
                "二维码登录会话异常结束",
                kind=WeiboErrorKind.UNKNOWN,
            )
        except OperationCancelled:
            with self._lock:
                session.state = "cancelled"
                session.message = "已取消扫码登录"
                session.qrcode_png = None
                session.result = None
            raise
        except WeiboError as exc:
            with self._lock:
                if session.state not in {"expired", "cancelled"}:
                    session.state = "error"
                    session.message = "扫码登录失败，请重新获取二维码"
                    session.qrcode_png = None
                    session.result = None
            raise exc
        except Exception as exc:
            logger.exception(
                "二维码登录会话异常: session_id=%s",
                session.session_id,
            )
            with self._lock:
                session.state = "error"
                session.message = "扫码登录失败，请重新获取二维码"
                session.qrcode_png = None
                session.result = None
            raise WeiboError(
                "扫码登录失败，请重新获取二维码",
                kind=WeiboErrorKind.UNKNOWN,
            ) from exc
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()
            with self._lock:
                session.qrcode_png = None
                session.closed_event.set()

    def _require(self, session_id: str) -> QRCodeLoginSession:
        session = self._session
        if session is None or session.session_id != session_id:
            raise WeiboError(
                "未找到二维码登录会话",
                kind=WeiboErrorKind.NOT_FOUND,
            )
        return session

    def _raise_if_cancelled(self, session: QRCodeLoginSession) -> None:
        if session.cancel_event.is_set():
            raise OperationCancelled("二维码登录已取消")

    def _raise_if_expired(self, session: QRCodeLoginSession) -> None:
        if self._monotonic() < session.deadline:
            return
        with self._lock:
            session.state = "expired"
            session.message = "二维码已过期，请重新获取"
            session.qrcode_png = None
            session.result = None
        raise WeiboError(
            "二维码已过期，请重新获取",
            kind=WeiboErrorKind.AUTH,
        )

    def _accept_qrcode(
        self,
        session: QRCodeLoginSession,
        payload: object | None,
    ) -> None:
        if (
            not isinstance(payload, bytes)
            or not payload.startswith(PNG_SIGNATURE)
            or len(payload) > MAX_QRCODE_PNG_BYTES
        ):
            raise WeiboError(
                "二维码图片无效",
                kind=WeiboErrorKind.PARSE,
            )
        with self._lock:
            session.qrcode_png = bytes(payload)
            session.state = "waiting_scan"
            session.message = "请使用微博 App 扫描二维码"

    def _set_state(
        self,
        session: QRCodeLoginSession,
        state: str,
        message: str,
    ) -> None:
        with self._lock:
            session.state = state
            session.message = message

    @staticmethod
    def _playwright_events(
        control: QRCodeLoginControl,
    ) -> Iterator[QRCodeLoginEvent]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            control.raise_if_cancelled()
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            pages = []
            try:
                control.raise_if_cancelled()
                landing = context.new_page()
                pages.append(landing)
                landing.goto(
                    PRIMARY_LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                control.raise_if_cancelled()
                landing.wait_for_timeout(8000)
                control.raise_if_cancelled()

                login_page = landing
                trigger = landing.get_by_role(
                    "button",
                    name="登录/注册",
                    exact=True,
                )
                if trigger.count() == 1:
                    try:
                        with context.expect_page(timeout=10000) as popup_info:
                            trigger.click()
                        login_page = popup_info.value
                        pages.append(login_page)
                        control.raise_if_cancelled()
                    except PlaywrightTimeoutError:
                        if "passport.weibo.com" not in landing.url:
                            landing.goto(
                                FALLBACK_LOGIN_URL,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            control.raise_if_cancelled()
                else:
                    landing.goto(
                        FALLBACK_LOGIN_URL,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    control.raise_if_cancelled()

                login_page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=30000,
                )
                control.raise_if_cancelled()
                login_page.wait_for_selector(
                    QRCODE_SELECTOR,
                    state="visible",
                    timeout=15000,
                )
                control.raise_if_cancelled()
                login_page.wait_for_timeout(1000)
                control.raise_if_cancelled()

                qrcode = login_page.locator(QRCODE_SELECTOR)
                heading = login_page.get_by_text(
                    QRCODE_HEADING,
                    exact=True,
                )
                if qrcode.count() != 1 or heading.count() != 1:
                    raise WeiboError(
                        "无法唯一确认微博二维码节点",
                        kind=WeiboErrorKind.PARSE,
                    )
                box = qrcode.bounding_box()
                if (
                    box is None
                    or box.get("width", 0) < 100
                    or box.get("height", 0) < 100
                    or abs(box["width"] - box["height"]) > 2
                ):
                    raise WeiboError(
                        "微博二维码节点尺寸无效",
                        kind=WeiboErrorKind.PARSE,
                    )
                baseline_fingerprint = _cookie_fingerprint(context.cookies())
                login_url = login_page.url
                last_validation_at = 0.0
                yield QRCodeLoginEvent.qrcode(qrcode.screenshot(type="png"))

                while (
                    not control.cancel_requested()
                    and control.remaining_seconds() > 0
                ):
                    # 每轮同时复核已取证的登录提示与二维码节点；
                    # 节点变化不能单独代表登录成功，最终仍只信服务端校验。
                    login_prompt_visible = (
                        heading.count() == 1 and qrcode.count() == 1
                    )
                    cookies = context.cookies()
                    cookie_changed = (
                        _cookie_fingerprint(cookies) != baseline_fingerprint
                    )
                    login_hint = (
                        login_page.url != login_url or not login_prompt_visible
                    )
                    now = time.monotonic()
                    if (
                        (cookie_changed or login_hint)
                        and now - last_validation_at >= 3
                    ):
                        last_validation_at = now
                        yield QRCodeLoginEvent.cookies(cookies)
                    login_page.wait_for_timeout(1000)
                    control.raise_if_cancelled()
                    yield QRCodeLoginEvent.tick()
            finally:
                for page in reversed(pages):
                    try:
                        page.close()
                    except Exception:
                        logger.debug("关闭二维码页面失败", exc_info=True)
                try:
                    context.close()
                except Exception:
                    logger.debug("关闭二维码 context 失败", exc_info=True)
                try:
                    browser.close()
                except Exception:
                    logger.debug("关闭二维码 browser 失败", exc_info=True)


qrcode_login_service = QRCodeLoginService()
