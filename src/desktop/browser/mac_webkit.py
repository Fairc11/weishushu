"""Native macOS WKWebView overlaid into a frontend-owned content slot."""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Optional

from .policy import is_allowed_browser_url, is_allowed_weibo_host
from .state import BrowserFrame, fit_browser_frame, parse_browser_frame

logger = logging.getLogger(__name__)

HOME_URL = "https://m.weibo.cn/"


if sys.platform == "darwin":
    import AppKit
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper

    def website_data_store_for_profile():
        """按唯一运行 profile 选择正式持久化或开发非持久化数据存储。"""
        from backend.app.profile import is_dev_profile

        if is_dev_profile():
            return WebKit.WKWebsiteDataStore.nonPersistentDataStore()
        return WebKit.WKWebsiteDataStore.defaultDataStore()

    def cookie_record_to_ns_cookie(record: dict):
        """把安全文件中的标准 Cookie 记录恢复为 `NSHTTPCookie`。"""
        properties = {
            Foundation.NSHTTPCookieName: str(record['name']),
            Foundation.NSHTTPCookieValue: str(record['value']),
            Foundation.NSHTTPCookieDomain: str(record['domain']),
            Foundation.NSHTTPCookiePath: str(record.get('path') or '/'),
        }
        if record.get('secure'):
            properties[Foundation.NSHTTPCookieSecure] = "TRUE"
        if record.get('httpOnly'):
            properties["HttpOnly"] = "TRUE"
        expires = record.get('expires')
        if isinstance(expires, (int, float)) and expires > 0:
            properties[Foundation.NSHTTPCookieExpires] = Foundation.NSDate.dateWithTimeIntervalSince1970_(expires)
        same_site = record.get('sameSite')
        if same_site:
            properties[Foundation.NSHTTPCookieSameSitePolicy] = str(same_site)
        cookie = Foundation.NSHTTPCookie.cookieWithProperties_(properties)
        if cookie is None:
            raise ValueError("Cookie 记录无法恢复")
        return cookie

    class _NavigationDelegate(Foundation.NSObject):
        def initWithController_(self, controller):
            self = self.init()
            if self is None:
                return None
            self.controller = controller
            return self

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, decision_handler):
            url = str(action.request().URL().absoluteString() or "")
            if is_allowed_browser_url(url):
                decision_handler(WebKit.WKNavigationActionPolicyAllow)
                return
            self.controller._set_status("已拦截非微博页面")
            decision_handler(WebKit.WKNavigationActionPolicyCancel)

        def webView_didFinishNavigation_(self, webview, navigation):
            self.controller._navigation_finished()

        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, webview, configuration, action, features
        ):
            url = str(action.request().URL().absoluteString() or "")
            if is_allowed_browser_url(url):
                webview.loadRequest_(action.request())
            else:
                self.controller._set_status("已拦截非微博页面")
            return None


class MacWebKitBrowserController:
    """Own the isolated Weibo view while the frontend owns all surrounding UI."""

    def __init__(
        self,
        cookie_sink: Optional[Callable[[list[dict]], bool]] = None,
        cookie_source: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.cookie_sink = cookie_sink
        self.cookie_source = cookie_source
        self._attached = False
        self._requested_visible = False
        self._slot_frame: Optional[BrowserFrame] = None
        self._native_window = None
        self._root_view = None
        self._main_webview = None
        self._browser_webview = None
        self._browser_delegate = None
        self._current_url: Optional[str] = HOME_URL
        self._last_auto_sync_at = 0.0

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, pywebview_window: Any, timeout: float = 8.0) -> dict:
        if sys.platform != "darwin":
            return {"ok": False, "error": "内嵌 WebKit 仅支持 macOS"}
        if self._attached:
            return {"ok": True, "url": self.current_url(), "embedded": True}

        completed = threading.Event()
        errors: list[str] = []

        def attach_on_main() -> None:
            try:
                self._attach_native(pywebview_window)
            except Exception as exc:
                logger.exception("Mac 内嵌浏览器挂载失败: %s", exc)
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                completed.set()

        AppHelper.callAfter(attach_on_main)
        if not completed.wait(timeout):
            return {"ok": False, "error": "Mac 内嵌浏览器挂载超时"}
        if errors:
            return {"ok": False, "error": errors[0]}
        return {"ok": True, "url": self.current_url(), "embedded": True}

    def _attach_native(self, pywebview_window: Any) -> None:
        native_window = getattr(pywebview_window, "native", None)
        if native_window is None:
            raise RuntimeError("pywebview 原生窗口尚未就绪")
        main_webview = native_window.contentView()
        if main_webview is None:
            raise RuntimeError("pywebview 主 WebView 不存在")

        frame = main_webview.frame()
        root_view = AppKit.NSView.alloc().initWithFrame_(frame)
        root_view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        main_webview.removeFromSuperview()
        main_webview.setFrame_(root_view.bounds())
        main_webview.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        root_view.addSubview_(main_webview)

        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(website_data_store_for_profile())
        browser_webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            Foundation.NSMakeRect(0, 0, 1, 1),
            config,
        )
        delegate = _NavigationDelegate.alloc().initWithController_(self)
        browser_webview.setNavigationDelegate_(delegate)
        browser_webview.setUIDelegate_(delegate)
        browser_webview.setHidden_(True)
        browser_webview.setWantsLayer_(True)
        if browser_webview.layer() is not None:
            browser_webview.layer().setCornerRadius_(10.0)
            browser_webview.layer().setMasksToBounds_(True)
        root_view.addSubview_(browser_webview)

        native_window.setContentView_(root_view)
        native_window.makeFirstResponder_(main_webview)

        self._native_window = native_window
        self._root_view = root_view
        self._main_webview = main_webview
        self._browser_webview = browser_webview
        self._browser_delegate = delegate
        self._attached = True
        self._apply_frame()
        self._restore_saved_cookies_then_load()

    def _restore_saved_cookies_then_load(self) -> None:
        records = []
        if self.cookie_source is not None:
            try:
                stored = self.cookie_source()
                if isinstance(stored, dict):
                    stored = stored.get("cookies") or []
                if isinstance(stored, list):
                    records = stored
            except Exception as exc:
                logger.warning("Mac 内嵌浏览器: 读取已保存登录失败: %s", exc)

        cookies = []
        for record in records:
            try:
                cookies.append(cookie_record_to_ns_cookie(record))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("Mac 内嵌浏览器: 忽略无效 Cookie 记录: %s", exc)
        if not cookies:
            self.load(HOME_URL)
            return

        store = self._browser_webview.configuration().websiteDataStore().httpCookieStore()
        pending = {"count": len(cookies)}

        def restored() -> None:
            pending["count"] -= 1
            if pending["count"] == 0:
                self._set_status(f"已恢复 {len(cookies)} 个登录 Cookie")
                self.load(HOME_URL)

        for cookie in cookies:
            store.setCookie_completionHandler_(cookie, restored)

    def set_frame(self, value: dict) -> bool:
        frame = parse_browser_frame(value)
        self._slot_frame = frame
        self._requested_visible = frame.visible
        if self._attached:
            AppHelper.callAfter(self._apply_frame)
        return True

    def _apply_frame(self) -> None:
        if not self._attached or self._root_view is None or self._browser_webview is None:
            return
        frame = self._slot_frame
        if frame is None or not self._requested_visible or not frame.visible:
            self._browser_webview.setHidden_(True)
            return

        bounds = self._root_view.bounds()
        fitted = fit_browser_frame(frame, bounds.size.width, bounds.size.height)
        if fitted is None:
            self._browser_webview.setHidden_(True)
            return
        self._browser_webview.setFrame_(Foundation.NSMakeRect(*fitted))
        self._browser_webview.setHidden_(False)

    def show(self) -> dict:
        if not self._attached:
            return {"ok": False, "error": "Mac 内嵌浏览器尚未就绪", "url": None}
        self._requested_visible = True
        if self._slot_frame is not None:
            self._slot_frame = replace(self._slot_frame, visible=True)
        AppHelper.callAfter(self._apply_frame)
        return {"ok": True, "url": self.current_url() or HOME_URL}

    def hide(self) -> bool:
        if not self._attached:
            return False
        self._requested_visible = False
        if self._slot_frame is not None:
            self._slot_frame = replace(self._slot_frame, visible=False)
        AppHelper.callAfter(self._apply_frame)
        return True

    def load(self, url: str) -> bool:
        if not self._attached or not is_allowed_browser_url(url):
            return False
        self._current_url = url

        def load_on_main() -> None:
            request = Foundation.NSURLRequest.requestWithURL_(Foundation.NSURL.URLWithString_(url))
            self._browser_webview.loadRequest_(request)

        AppHelper.callAfter(load_on_main)
        return True

    def current_url(self) -> Optional[str]:
        return self._current_url if self._attached else None

    def _remember_current_url(self) -> None:
        if self._browser_webview is None:
            return
        url = self._browser_webview.URL()
        self._current_url = str(url.absoluteString()) if url is not None else HOME_URL

    def _navigation_finished(self) -> None:
        self._remember_current_url()
        now = time.monotonic()
        if now - self._last_auto_sync_at < 5.0:
            return
        self._last_auto_sync_at = now
        self.export_weibo_cookies()

    def back(self) -> bool:
        if not self._attached:
            return False
        AppHelper.callAfter(self._browser_webview.goBack)
        return True

    def forward(self) -> bool:
        if not self._attached:
            return False
        AppHelper.callAfter(self._browser_webview.goForward)
        return True

    def reload(self) -> bool:
        if not self._attached:
            return False
        AppHelper.callAfter(self._browser_webview.reload)
        return True

    def copy_url_to_clipboard(self) -> bool:
        url = self.current_url()
        if not url:
            return False

        def copy_on_main() -> None:
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            pasteboard.setString_forType_(url, AppKit.NSPasteboardTypeString)

        AppHelper.callAfter(copy_on_main)
        self._set_status("已复制 URL")
        return True

    def _set_status(self, text: str) -> None:
        logger.info("Mac 内嵌浏览器: %s", text)

    def export_weibo_cookies(self, callback: Optional[Callable[[list[dict]], bool]] = None) -> bool:
        callback = callback or self.cookie_sink
        if not self._attached or callback is None:
            self._set_status("登录同步不可用")
            return False

        store = self._browser_webview.configuration().websiteDataStore().httpCookieStore()

        def receive(cookies) -> None:
            exported = []
            for cookie in cookies:
                domain = str(cookie.domain() or "")
                if not is_allowed_weibo_host(domain.lstrip(".")):
                    continue
                exported.append({
                    "name": str(cookie.name() or ""),
                    "value": str(cookie.value() or ""),
                    "domain": domain,
                    "path": str(cookie.path() or "/"),
                    "expires": (
                        float(cookie.expiresDate().timeIntervalSince1970())
                        if cookie.expiresDate() is not None else None
                    ),
                    "secure": bool(cookie.isSecure()),
                    "httpOnly": bool(cookie.isHTTPOnly()),
                    "sameSite": str(cookie.sameSitePolicy() or "") or None,
                })

            def persist() -> None:
                ok = bool(exported) and bool(callback(exported))
                self._set_status("登录已同步" if ok else "登录校验失败")

            threading.Thread(target=persist, daemon=True).start()

        store.getAllCookies_(receive)
        return True

    def close(self, *_args) -> None:
        if self._browser_webview is not None:
            self._browser_webview.setNavigationDelegate_(None)
            self._browser_webview.setUIDelegate_(None)
            self._browser_webview.removeFromSuperview()
        self._browser_delegate = None
        self._browser_webview = None
        self._attached = False
