"""pywebview → Python 桥。前端通过 window.pywebview.api.xxx() 调用原生能力。

阶段 1 只暴露最小集合（get_version / minimize / close_window）让前端能探测到桌面壳。
阶段 2 加 open_folder / show_in_folder / pick_output_dir。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import webview  # type: ignore[import-untyped]


def _open_in_explorer(path: str) -> None:
    """资源管理器打开目录 / 选中文件（仅 Windows）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    if sys.platform != "win32":
        # macOS / Linux 兜底
        if p.is_dir():
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p.parent)])
        return
    # Windows: explorer 接受 file/dir；文件加 /select 高亮
    if p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["explorer", str(p)])


def _pywebview_cookies_to_records(cookies: Any) -> list[dict]:
    """把 pywebview Window.get_cookies() 的 SimpleCookie 转成登录模块记录。"""
    records: list[dict] = []
    for cookie in cookies or []:
        values = getattr(cookie, "values", None)
        morsels = list(values()) if callable(values) else []
        for morsel in morsels:
            name = str(getattr(morsel, "key", "") or "")
            value = str(getattr(morsel, "value", "") or "")
            if not name or not value:
                continue
            record = {
                "name": name,
                "value": value,
                "domain": str(morsel["domain"] or ""),
                "path": str(morsel["path"] or "/"),
            }
            expires = morsel["expires"] or None
            if expires:
                record["expires"] = expires
            secure = morsel["secure"]
            if secure is not None:
                record["secure"] = bool(secure)
            http_only = morsel["httponly"]
            if http_only is not None:
                record["httpOnly"] = bool(http_only)
            same_site = morsel["samesite"] or None
            if same_site:
                record["sameSite"] = str(same_site)
            records.append(record)
    return records


class JsApi:
    """pywebview 暴露给 JS 的方法集。每个方法都加 try/except 兜底，
    避免原生调用炸了把 pywebview 整个搞挂。"""

    def __init__(self, *, task_manager: Any = None) -> None:
        self._window: Optional["webview.Window"] = None
        # v1.2.0 V120-3: 浏览器窗口引用（独立 webview），用于 cookie 注入
        self._browser_window: Optional["webview.Window"] = None
        self._browser_controller: Optional[Any] = None
        self._browser_window_sync_lock = threading.Lock()
        self._browser_window_last_sync_at = 0.0
        self._task_manager = task_manager
        self._close_permission_lock = threading.Lock()
        self._allow_close_once = False

    def _manager(self) -> Any:
        if self._task_manager is not None:
            return self._task_manager
        from backend.app.services.task_manager import task_manager
        return task_manager

    def set_window(self, window: "webview.Window") -> None:
        self._window = window

    def set_browser_controller(self, controller: Any) -> None:
        """注入 Mac 同窗内嵌浏览器；Windows 保持为 None。"""
        self._browser_controller = controller

    def set_browser_frame(self, frame: dict) -> dict:
        """将前端原生内容槽矩形同步给 Mac 浏览器控制器。"""
        if self._browser_controller is None:
            return {"ok": False, "error": "当前平台没有内嵌浏览器"}
        try:
            return {"ok": bool(self._browser_controller.set_frame(frame))}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def sync_browser_login(self) -> dict:
        """从当前内置浏览器导出微博 Cookie，校验后写入本机登录文件。"""
        if self._browser_controller is not None:
            return {"ok": bool(self._browser_controller.export_weibo_cookies())}
        if self._browser_window is None:
            return {"ok": False, "error": "浏览器窗口未打开，请先打开内置浏览器"}
        return self._sync_browser_window_login(manual=True)

    def _sync_browser_window_login(self, *, manual: bool = False) -> dict:
        with self._browser_window_sync_lock:
            window = self._browser_window
            if window is None:
                return {"ok": False, "error": "浏览器窗口未打开，请先打开内置浏览器"}
            now = time.monotonic()
            if not manual and now - self._browser_window_last_sync_at < 5.0:
                return {"ok": False, "error": "同步过于频繁", "skipped": True}
            self._browser_window_last_sync_at = now
            try:
                cookies = window.get_cookies()
            except Exception as exc:
                logger.warning("读取内置浏览器 Cookie 失败: %s", exc)
                return {"ok": False, "error": f"读取内置浏览器登录状态失败: {exc}"}
            records = _pywebview_cookies_to_records(cookies)
            if not records:
                return {"ok": False, "error": "内置浏览器还没有可同步的微博登录状态"}

            from weibo_book.login import check_cookies_valid, save_cookies

            if not check_cookies_valid(records):
                return {"ok": False, "error": "内置浏览器登录状态未通过微博校验"}
            saved = save_cookies(records)
            logger.info("内置浏览器登录已同步（%d 个 Cookie）", len(saved))
            return {"ok": True, "count": len(saved)}

    def _on_browser_window_loaded(self) -> None:
        threading.Thread(
            target=self._sync_browser_window_login,
            kwargs={"manual": False},
            name="weishushu-browser-login-sync",
            daemon=True,
        ).start()

    def _on_browser_window_closed(self) -> None:
        self._browser_window = None

    # ====== 元信息 ======
    def get_version(self) -> str:
        # 延迟 import 避免 webview 没装时 import 失败
        try:
            from backend.app.version import VERSION
            return VERSION
        except Exception:
            # B06 v1.2.0: 与 backend/app/version.py VERSION 保持一致（兜底）
            return "2.0.1"

    def get_platform(self) -> str:
        return sys.platform

    # ====== 窗口控制 ======
    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._window is not None:
            self._window.toggle_fullscreen()

    def close_window(self) -> bool:
        if self._manager().active_persistent_task() is not None:
            if self._window is not None:
                self._window.evaluate_js("Ptu.showCloseProtection()")
            return False
        if self._window is not None:
            self._window.destroy()
        return True

    def consume_close_permission(self) -> bool:
        with self._close_permission_lock:
            allowed = self._allow_close_once
            self._allow_close_once = False
            return allowed

    def close_after_pause(self, task_id: str) -> bool:
        record = self._manager().get(task_id)
        if (
            record is None
            or record.persistent_record is None
            or record.persistent_record.task_id != task_id
            or record.state != "waiting_resume"
        ):
            return False
        with self._close_permission_lock:
            self._allow_close_once = True
        if self._window is not None:
            self._window.destroy()
        return True

    # ====== 系统集成（阶段 2 用，阶段 1 占位） ======
    def open_folder(self, path: str) -> bool:
        try:
            _open_in_explorer(path)
            return True
        except Exception as e:
            logger.exception("open_folder 失败: %s", e)
            return False

    def show_in_folder(self, path: str) -> bool:
        return self.open_folder(path)

    # ====== v1.1.5 一键备份：选目录 ======
    def select_folder(self, default_path: str = "") -> Optional[str]:
        """v1.1.5 桥：pywebview 5.x 弹系统目录选择对话框。"""
        try:
            if self._window is None:
                return None
            import webview  # type: ignore[import-untyped]
            initial = default_path if default_path and Path(default_path).is_dir() else str(Path.home())
            if sys.platform == "darwin":
                return self._select_folder_macos(initial)
            result = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=initial
            )
            # pywebview 5.x 返 tuple[str] | tuple[()]，取第一个
            if result and len(result) > 0:
                return str(result[0])
            return None
        except Exception as e:
            logger.exception("select_folder 失败: %s", e)
            return None

    def _select_folder_macos(self, initial: str) -> Optional[str]:
        """使用 AppKit 公开接口显示简体中文目录面板。"""
        import AppKit
        import Foundation
        from PyObjCTools import AppHelper

        selected: list[Optional[str]] = [None]
        finished = threading.Event()

        def show_panel() -> None:
            try:
                panel = AppKit.NSOpenPanel.openPanel()
                panel.setTitle_("选择备份文件夹")
                panel.setMessage_("请选择微博备份的保存位置")
                panel.setPrompt_("选择")
                panel.setCanChooseFiles_(False)
                panel.setCanChooseDirectories_(True)
                panel.setCanCreateDirectories_(True)
                panel.setAllowsMultipleSelection_(False)
                panel.setDirectoryURL_(Foundation.NSURL.fileURLWithPath_(initial))
                if panel.runModal() == AppKit.NSModalResponseOK:
                    urls = panel.URLs()
                    if urls and len(urls) > 0:
                        selected[0] = str(urls[0].path())
            finally:
                finished.set()

        if Foundation.NSThread.isMainThread():
            show_panel()
        else:
            AppHelper.callAfter(show_panel)
            finished.wait()
        return selected[0]

    def open_backup_folder(self, path: str) -> bool:
        """v1.1.5：备份完成后"打开输出文件夹"专用入口。"""
        return self.open_folder(path)

    def open_active_task_folder(self, task_id: str) -> bool:
        """按精确任务标识打开持久任务的正式输出目录。"""
        try:
            from backend.app.services.task_manager import task_manager

            record = task_manager.get(task_id)
            if (
                record is None
                or record.persistent_record is None
                or record.persistent_record.task_id != task_id
            ):
                return False
            return self.open_folder(record.persistent_record.output_dir)
        except Exception:
            logger.exception("open_active_task_folder 失败")
            return False

    # ====== 从浏览器复制 URL 到主流程 ======

    # 类属性：最近一次复制的 URL（前端轮询 + 一次性消费）
    _last_copied_url: Optional[str] = None

    def copy_url_to_main(self, url: Optional[str] = None) -> dict:
        """V120-5: 把浏览器 URL 复制到主区 URL 输入框。

        Args:
            url: 可选，URL 字符串。None 时从 _browser_window 拿当前 location.href。

        Returns:
            {ok, url} 或 {ok: false, error}
        """
        if url is None:
            if self._browser_controller is not None:
                url = self._browser_controller.current_url()
            elif self._browser_window is None:
                return {"ok": False, "error": "浏览器窗口未打开，请先点 🌐 浏览器"}
            else:
                try:
                    url = self._browser_window.evaluate_js("window.location.href")
                except Exception as e:
                    return {"ok": False, "error": f"读 URL 失败: {e}"}

        if not url:
            return {"ok": False, "error": "URL 为空"}

        url_str = str(url).strip()

        from desktop.browser.policy import is_allowed_browser_url

        if not is_allowed_browser_url(url_str):
            return {"ok": False, "error": f"非微博链接，已忽略: {url_str}"}

        # 存到类属性，前端轮询 + 一次性消费
        JsApi._last_copied_url = url_str
        logger.info("URL 已复制到主区: %s", url_str)
        return {"ok": True, "url": url_str}

    def get_copied_url(self) -> Optional[str]:
        """前端轮询拿最近复制的 URL（一次性消费）。"""
        url = JsApi._last_copied_url
        JsApi._last_copied_url = None
        return url

    # ====== 浏览器窗口：Mac 同窗控制器 / Windows 历史独立窗口 ======

    def open_browser_window(self) -> dict:
        """Mac 展开同窗 WKWebView；其他平台保留独立 pywebview 窗口。

        Mac 控制器存在时不创建第二个 pywebview Window。Windows 回退路径仍保存
        `_browser_window`，并在重开前销毁旧窗口，避免多窗引用竞争。
        """
        from backend.app.features import (
            EMBEDDED_WEIBO_BROWSER_ENABLED,
            FUTURE_FEATURE_MESSAGE,
        )

        if not EMBEDDED_WEIBO_BROWSER_ENABLED:
            return {"ok": False, "error": FUTURE_FEATURE_MESSAGE}
        if self._browser_controller is not None:
            result = self._browser_controller.show()
            if isinstance(result, dict):
                return {**result, "embedded": True}
            return {
                "ok": bool(result),
                "url": self._browser_controller.current_url(),
                "embedded": True,
            }

        try:
            import webview  # type: ignore[import-untyped]
        except ImportError:
            return {"ok": False, "error": "pywebview 不可用（仅桌面版支持）", "url": None}

        # B13 v1.2.0: 防止重复开窗。先 destroy 已存在的，再建新的（UX 更好：刷新即重建）
        if self._browser_window is not None:
            try:
                self._browser_window.destroy()
            except Exception as e:
                logger.warning("destroy 旧浏览器窗口失败（忽略继续）: %s", e)
            self._browser_window = None

        try:
            window = webview.create_window(
                title="m.weibo.cn · 微书薯内置浏览器",
                url="https://m.weibo.cn",
                width=480,
                height=820,
                resizable=True,
                text_select=True,
            )
            # V120-3: 保存引用，cookie 注入与登录同步用
            self._browser_window = window
            self._browser_window_last_sync_at = 0.0
            window.events.loaded += self._on_browser_window_loaded
            window.events.closed += self._on_browser_window_closed
            return {
                "ok": True,
                "url": "https://m.weibo.cn",
                "title": "m.weibo.cn · 微书薯内置浏览器",
            }
        except Exception as e:
            logger.exception("open_browser_window 失败: %s", e)
            return {"ok": False, "error": str(e), "url": None}

    # ====== v1.2.0 V120-3: cookie 注入 ======

    def inject_cookies(self, cookies: Optional[list] = None) -> dict:
        """V120-3: 把 cookies 注入到浏览器窗口。

        Args:
            cookies: 可选，list[dict{name, value, domain, path}]。None 时自己读 cookies.json。

        Returns:
            {ok, success, failed, failed_names, total}
        """
        if self._browser_window is None:
            return {
                "ok": False,
                "error": "浏览器窗口未打开，请先点 🌐 浏览器",
                "success": 0,
                "failed": 0,
            }

        if cookies is None:
            cookies = self._read_cookies_from_disk()
            if cookies is None:
                return {
                    "ok": False,
                    "error": "cookies.json 不存在或解析失败",
                    "success": 0,
                    "failed": 0,
                }

        success = 0
        failed = 0
        failed_names: list[str] = []

        for c in cookies:
            name = c.get("name", "").strip()
            value = c.get("value", "").strip()
            if not name or not value:
                continue
            domain = c.get("domain", ".weibo.cn")
            path = c.get("path", "/")
            # B02 v1.2.0: 用 json.dumps 转义防注入 + 防单引号/反斜杠/换行符破 JS 字符串
            # json.dumps 默认把 ' 转义为 \'，把 " 转义为 \"，把 \ 转义为 \\
            js = (
                "document.cookie = "
                + json.dumps(f"{name}={value}; domain={domain}; path={path}", ensure_ascii=False)
                + ";"
            )
            try:
                self._browser_window.evaluate_js(js)
                success += 1
            except Exception as e:
                logger.warning("注入 cookie %s 失败: %s", name, e)
                failed += 1
                failed_names.append(name)

        return {
            "ok": success > 0,
            "success": success,
            "failed": failed,
            "failed_names": failed_names,
            "total": success + failed,
        }

    def _read_cookies_from_disk(self) -> Optional[list]:
        """按统一候选列表读取 cookie 文件。"""
        from backend.app.platform_paths import cookie_file_candidates

        for cookie_path in cookie_file_candidates():
            try:
                if not cookie_path.exists():
                    continue
                data = json.loads(cookie_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return [
                        {"name": k, "value": v, "domain": ".weibo.cn", "path": "/"}
                        for k, v in data.items()
                    ]
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.warning("读 cookies.json 失败 (%s): %s", cookie_path, e)
                continue
        return None

    # ====== v1.2.0 收口 A: 浏览器控制台 5 按钮 ======

    def refresh_browser(self) -> dict:
        """V120-2: 刷新浏览器窗口（window.location.reload）。"""
        if self._browser_controller is not None:
            return {"ok": bool(self._browser_controller.reload())}
        if self._browser_window is None:
            return {"ok": False, "error": "浏览器窗口未打开"}
        try:
            self._browser_window.evaluate_js("window.location.reload()")
            return {"ok": True}
        except Exception as e:
            logger.exception("refresh_browser 失败: %s", e)
            return {"ok": False, "error": str(e)}

    def close_browser_window(self) -> dict:
        """V120-2: 关闭浏览器窗口。

        B14 v1.2.0: destroy 抛异常时也要把 _browser_window 清空（finally），
        避免后续 open_browser_window 误判为"已开"导致 destroy 旧窗时引用悬空。
        """
        if self._browser_controller is not None:
            return {"ok": bool(self._browser_controller.hide())}
        if self._browser_window is None:
            return {"ok": False, "error": "浏览器窗口未打开"}
        try:
            self._browser_window.destroy()
            return {"ok": True}
        except Exception as e:
            logger.exception("close_browser_window 失败: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            # B14 v1.2.0: 无论 destroy 成败都清空引用
            self._browser_window = None

    def get_browser_current_url(self) -> Optional[str]:
        """V120-2 收口：拿浏览器当前 URL（前端控制台显示用）。"""
        if self._browser_controller is not None:
            return self._browser_controller.current_url()
        if self._browser_window is None:
            return None
        try:
            url = self._browser_window.evaluate_js("window.location.href")
            return str(url) if url else None
        except Exception as e:
            logger.warning("get_browser_current_url 失败: %s", e)
            return None

    def browser_back(self) -> dict:
        """V120-2: 浏览器后退（window.history.back）。"""
        if self._browser_controller is not None:
            return {"ok": bool(self._browser_controller.back())}
        if self._browser_window is None:
            return {"ok": False, "error": "浏览器窗口未打开"}
        try:
            self._browser_window.evaluate_js("window.history.back()")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def browser_forward(self) -> dict:
        """V120-2: 浏览器前进（window.history.forward）。"""
        if self._browser_controller is not None:
            return {"ok": bool(self._browser_controller.forward())}
        if self._browser_window is None:
            return {"ok": False, "error": "浏览器窗口未打开"}
        try:
            self._browser_window.evaluate_js("window.history.forward()")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
