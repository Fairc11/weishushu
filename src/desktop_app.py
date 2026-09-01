"""pywebview 桌面壳：找空闲端口 → 后台起 uvicorn → 弹窗口。"""

from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Any, List, Optional

from js_api import JsApi

# v1.1.6 装得开打不开修复：frozen 模式下日志也写盘（用户看不到 console）
def _setup_logging() -> None:
    """dev 模式 console 输出；frozen 模式额外写盘到 %LOCALAPPDATA%\\Weishushu\\logs\\。

    B07 v1.2.0: 日志目录统一从 backend.app.config.settings.log_dir 拿（全英文子目录 logs/），
    不再硬编码 %LOCALAPPDATA%\\Weishushu\\日志\\，避免中文字符在 GBK 终端下的问题。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if getattr(sys, "frozen", False):
        # 延迟 import 避免打包/依赖顺序问题
        from backend.app.config import settings
        log_dir = settings.log_dir
        # v1.1.6：每次启动带时间戳的 log 文件，process kill 时不丢
        fh = logging.FileHandler(log_dir / f"boot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
                                encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

_setup_logging()
logger = logging.getLogger("desktop")

CHINESE_LOCALIZATION = {
    "global.quitConfirmation": "确定要退出吗？",
    "global.ok": "确定",
    "global.quit": "退出",
    "global.cancel": "取消",
    "global.saveFile": "保存文件",
    "cocoa.menu.about": "关于",
    "cocoa.menu.services": "服务",
    "cocoa.menu.view": "显示",
    "cocoa.menu.edit": "编辑",
    "cocoa.menu.hide": "隐藏",
    "cocoa.menu.hideOthers": "隐藏其他",
    "cocoa.menu.showAll": "全部显示",
    "cocoa.menu.quit": "退出",
    "cocoa.menu.fullscreen": "进入全屏幕",
    "cocoa.menu.cut": "剪切",
    "cocoa.menu.copy": "复制",
    "cocoa.menu.paste": "粘贴",
    "cocoa.menu.selectAll": "全选",
    "windows.fileFilter.allFiles": "所有文件",
    "windows.fileFilter.otherFiles": "其他文件类型",
    "linux.openFile": "打开文件",
    "linux.openFiles": "打开多个文件",
    "linux.openFolder": "打开文件夹",
}


def load_webview() -> Optional[Any]:
    """延迟导入 pywebview，避免后端/测试仅导入 desktop_app 时被 GUI 依赖阻塞。"""
    try:
        import webview  # type: ignore[import-untyped]
        return webview
    except ImportError as exc:
        logger.error("pywebview 不可用，请先安装 Mac 依赖: %s", exc)
        return None


def select_gui_backend(is_frozen: bool, platform: str = sys.platform) -> Optional[str]:
    """Windows frozen 用 edgechromium；macOS/Linux 交给 pywebview 默认后端。"""
    if platform == "win32" and is_frozen:
        return "edgechromium"
    return None


def should_check_webview2(platform: str = sys.platform) -> bool:
    """WebView2 只属于 Windows 桌面壳。"""
    return platform == "win32"


def should_enable_debug(is_frozen: bool, environ: Optional[dict[str, str]] = None) -> bool:
    """开发版默认开检查器；可显式关闭，封装版始终关闭。"""
    if is_frozen:
        return False
    values = os.environ if environ is None else environ
    return values.get("WEISHUSHU_DEBUG", "1").strip().lower() not in {"0", "false", "no", "off"}


def resolve_window_size(environ: Optional[dict[str, str]] = None) -> tuple[int, int]:
    """Allow exact visual-regression sizes without weakening the product minimum."""
    values = os.environ if environ is None else environ
    match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", values.get("WEISHUSHU_WINDOW_SIZE", ""))
    if match is None:
        return (1280, 820)
    width, height = (int(match.group(1)), int(match.group(2)))
    if width < 960 or height < 640:
        return (1280, 820)
    return (width, height)


def _persist_embedded_browser_cookies(cookies: list[dict]) -> bool:
    """校验并保存 Mac 内嵌 WebKit 产生的微博 Cookie。"""
    from weibo_book.login import check_cookies_valid, save_cookies

    if not check_cookies_valid(cookies):
        return False
    save_cookies(cookies)
    return True


def create_embedded_browser_controller(platform: str = sys.platform):
    """保留旧控制器构造入口，当前版本启动路径不创建。"""
    from backend.app.features import EMBEDDED_WEIBO_BROWSER_ENABLED

    if not EMBEDDED_WEIBO_BROWSER_ENABLED or platform != "darwin":
        return None
    from desktop.browser.mac_webkit import MacWebKitBrowserController
    from weibo_book.login import load_cookies

    return MacWebKitBrowserController(
        cookie_sink=_persist_embedded_browser_cookies,
        cookie_source=load_cookies,
    )


def find_free_port(start: int = 18080, end: int = 18180) -> int:
    """18080-18180 范围内找空闲端口。R4 风险缓解。"""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"未找到空闲端口 ({start}-{end})")


def start_backend(
    port_holder: List[int],
    ready_event: threading.Event,
    startup_errors: List[str],
) -> None:
    """后台线程起 uvicorn，并只在监听成功后通知主线程创建窗口。"""
    try:
        import uvicorn
        from backend.app.main import app

        port = find_free_port()
        port_holder.append(port)
        logger.info("backend starting on 127.0.0.1:%d", port)

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            log_level="warning",
            access_log=False,
        )

        class _ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):  # type: ignore[override]
                await super().startup(sockets)
                if self.started:
                    logger.info("uvicorn 已开始监听，set ready_event")
                else:
                    startup_errors.append("Uvicorn 未进入监听状态")
                    logger.error("uvicorn 启动后未进入监听状态")
                ready_event.set()

        _ReadyServer(config).run()
    except Exception as e:
        startup_errors.append(f"{type(e).__name__}: {e}")
        logger.exception("后端启动失败: %s", e)
        ready_event.set()


class DesktopCloseProtection:
    """拦截会造成无窗口后台任务的原生关闭。"""

    def __init__(self, manager, js_api: JsApi) -> None:
        self._manager = manager
        self._js_api = js_api

    def handle_closing(self) -> bool:
        if self._js_api.consume_close_permission():
            return True
        if self._manager.active_persistent_task() is None:
            return True
        window = self._js_api._window
        if window is not None:
            def show_protection() -> None:
                try:
                    window.evaluate_js("Ptu.showCloseProtection()")
                except Exception:
                    logger.exception("显示关闭保护对话框失败")

            threading.Thread(
                target=show_protection,
                name="weishushu-close-protection",
                daemon=True,
            ).start()
        return False


def main() -> int:
    webview = load_webview()
    if webview is None:
        return 4

    port_holder: List[int] = []
    ready_event = threading.Event()
    startup_errors: List[str] = []

    t = threading.Thread(
        target=start_backend,
        args=(port_holder, ready_event, startup_errors),
        daemon=True,
    )
    t.start()

    # 冷启动仍允许 12 秒，但不会因为超时兜底而在不可用端口上创建窗口。
    if not ready_event.wait(timeout=12.0):
        logger.error("后端启动超时 (>12s)")
        return 1
    if startup_errors:
        logger.error("后端启动失败: %s", startup_errors[0])
        return 1
    if not port_holder:
        logger.error("port_holder 为空，端口未分配")
        return 1
    port = port_holder[0]
    logger.info("backend ready, port=%d", port)

    install_path = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else ""
    is_program_files = install_path.upper().startswith(("C:\\PROGRAM FILES", "C:\\PROGRAM FILES (X86)"))
    logger.info("安装路径: %s (Program Files: %s)", install_path, is_program_files)

    js_api = JsApi()
    embedded_browser = create_embedded_browser_controller(sys.platform)
    if embedded_browser is not None:
        js_api.set_browser_controller(embedded_browser)
    try:
        window_width, window_height = resolve_window_size()
        try:
            from backend.app.profile import window_title
            window_title_text = window_title()
        except Exception:
            window_title_text = "微书薯"
        window = webview.create_window(
            title=window_title_text,
            url=f"http://127.0.0.1:{port}/",
            width=window_width,
            height=window_height,
            min_size=(960, 640),
            resizable=True,
            frameless=False,
            confirm_close=False,
            easy_drag=True,
            text_select=True,
            js_api=js_api,
            localization=CHINESE_LOCALIZATION,
        )
    except Exception as e:
        logger.exception("webview.create_window 失败: %s", e)
        return 2
    js_api.set_window(window)
    from backend.app.services.task_manager import task_manager
    close_protection = DesktopCloseProtection(task_manager, js_api)
    window.events.closing += close_protection.handle_closing
    if embedded_browser is not None:
        def attach_embedded_browser() -> None:
            result = embedded_browser.attach(window)
            if result.get("ok"):
                logger.info("Mac 内嵌微博浏览器已挂载")
            else:
                logger.error("Mac 内嵌微博浏览器不可用: %s", result.get("error"))

        window.events.loaded += attach_embedded_browser
        window.events.closing += embedded_browser.close

    is_frozen = getattr(sys, "frozen", False)
    gui_backend = select_gui_backend(is_frozen, sys.platform)
    # B04 v1.2.0: dev 才开 debug（DevTools 工具栏）；frozen 永远关 debug，避免给最终用户开 DevTools
    debug_enabled = should_enable_debug(is_frozen)
    logger.info("webview.start() 开始 (frozen=%s, gui=%s, debug=%s)", is_frozen, gui_backend or "default", debug_enabled)

    if should_check_webview2(sys.platform):
        try:
            from backend.app.services.setup_check import check_webview2_installed
            if not check_webview2_installed():
                logger.error("WebView2 Runtime 未检测到！请装 WebView2EvergreenBootstrapper.exe 后再启")
            else:
                logger.info("WebView2 Runtime 已装")
        except Exception as e:
            logger.warning("WebView2 检测失败: %s", e)
    else:
        logger.info("非 Windows 平台跳过 WebView2 检测")

    from backend.app.services.system_power import system_power_service
    system_power_service.start_observing()
    try:
        webview.start(debug=debug_enabled, gui=gui_backend)
    except Exception as e:
        logger.exception("webview.start 失败: %s", e)
        return 3
    finally:
        system_power_service.stop_observing()

    logger.info("webview 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
