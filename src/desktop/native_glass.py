"""macOS 26+ 原生液态玻璃：窗口背景与悬浮侧边栏由系统绘制，网页在其上方绘制内容。

- 只在 macOS 26 及以上、且 AppKit 提供 ``NSGlassEffectView`` 时启用；其他平台与旧系统保持普通窗口。
- 依赖 pywebview 6.2.1 Cocoa 后端的内部对象 ``BrowserView.instances``；升级 pywebview 时必须复验。
- 任何一步失败都只记录日志并保持普通外观：网页在未收到 ``data-native-glass`` 标记时自行绘制不透明背景。
"""
from __future__ import annotations

import logging
import platform
import sys
from typing import Any

logger = logging.getLogger(__name__)

# 与 backend/app/static/css/liquid.css 中 --lq-gap、--lq-side-w 与侧边栏圆角保持一致
SIDEBAR_INSET = 10.0
SIDEBAR_WIDTH = 232.0
SIDEBAR_RADIUS = 16.0
MIN_MACOS_MAJOR = 26

_MARK_PAGE_JS = "document.documentElement.dataset.nativeGlass='on'"


def macos_major_version(release: str | None = None) -> int:
    value = platform.mac_ver()[0] if release is None else release
    try:
        return int(str(value).split(".")[0])
    except (TypeError, ValueError):
        return 0


def native_glass_supported(
    platform_name: str = sys.platform, release: str | None = None
) -> bool:
    """当前系统能否使用原生液态玻璃。"""
    if platform_name != "darwin" or macos_major_version(release) < MIN_MACOS_MAJOR:
        return False
    try:
        import AppKit  # type: ignore[import-not-found]
    except Exception:
        return False
    return hasattr(AppKit, "NSGlassEffectView")


def _decorate(window: Any) -> bool:
    """在主线程执行：设置透明标题栏，并在网页下方加入窗口玻璃与侧边栏玻璃。"""
    import AppKit  # type: ignore[import-not-found]
    from webview.platforms.cocoa import BrowserView  # type: ignore[import-not-found]

    browser_view = BrowserView.instances.get(window.uid)
    if browser_view is None:
        logger.warning("原生玻璃：找不到窗口对象，保持普通外观")
        return False
    nswindow = browser_view.window
    web = browser_view.webview
    # pywebview 透明模式会关闭窗口阴影，这里重新开启
    nswindow.setHasShadow_(True)
    nswindow.setStyleMask_(nswindow.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    nswindow.setTitlebarAppearsTransparent_(True)
    nswindow.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    # 普通窗口模式下 pywebview 把标题栏背景涂成不透明，改为透明
    titlebar = nswindow.contentView().superview().subviews().lastObject()
    if titlebar is not None and hasattr(titlebar, "setBackgroundColor_"):
        titlebar.setBackgroundColor_(AppKit.NSColor.clearColor())

    bounds = web.bounds()
    background = AppKit.NSGlassEffectView.alloc().initWithFrame_(bounds)
    background.setCornerRadius_(0)
    background.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    web.addSubview_positioned_relativeTo_(background, AppKit.NSWindowBelow, None)

    sidebar = AppKit.NSGlassEffectView.alloc().initWithFrame_(
        AppKit.NSMakeRect(
            SIDEBAR_INSET,
            SIDEBAR_INSET,
            SIDEBAR_WIDTH,
            max(0.0, bounds.size.height - SIDEBAR_INSET * 2),
        )
    )
    sidebar.setCornerRadius_(SIDEBAR_RADIUS)
    sidebar.setAutoresizingMask_(AppKit.NSViewHeightSizable | AppKit.NSViewMaxXMargin)
    web.addSubview_positioned_relativeTo_(sidebar, AppKit.NSWindowAbove, background)
    return True


def install_native_glass(window: Any) -> None:
    """注册窗口事件：显示后接入原生玻璃，每次页面加载后通知网页改为透明背景。"""
    state = {"active": False}

    def mark_page() -> None:
        if not state["active"]:
            return
        try:
            window.evaluate_js(_MARK_PAGE_JS)
        except Exception as exc:  # 页面尚未就绪等情况，下一次加载时会再设置
            logger.warning("原生玻璃：通知页面失败: %s", exc)

    def on_shown() -> None:
        try:
            from PyObjCTools import AppHelper  # type: ignore[import-not-found]
        except Exception as exc:
            logger.warning("原生玻璃不可用，保持普通外观: %s", exc)
            return

        def run() -> None:
            try:
                state["active"] = _decorate(window)
            except Exception as exc:
                state["active"] = False
                logger.exception("原生玻璃接入失败，保持普通外观: %s", exc)
                return
            if state["active"]:
                logger.info("原生液态玻璃已启用")
                mark_page()

        AppHelper.callAfter(run)

    window.events.shown += on_shown
    window.events.loaded += mark_page


def set_window_appearance(window: Any, theme: str) -> bool:
    """让原生玻璃跟随 App 内的浅色 / 深色主题。"""
    if theme not in {"light", "dark"} or sys.platform != "darwin":
        return False
    try:
        import AppKit  # type: ignore[import-not-found]
        from PyObjCTools import AppHelper  # type: ignore[import-not-found]
        from webview.platforms.cocoa import BrowserView  # type: ignore[import-not-found]
    except Exception:
        return False
    name = AppKit.NSAppearanceNameDarkAqua if theme == "dark" else AppKit.NSAppearanceNameAqua

    def run() -> None:
        browser_view = BrowserView.instances.get(window.uid)
        if browser_view is not None:
            browser_view.window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name))

    try:
        AppHelper.callAfter(run)
        return True
    except Exception as exc:
        logger.warning("原生玻璃：切换外观失败: %s", exc)
        return False
