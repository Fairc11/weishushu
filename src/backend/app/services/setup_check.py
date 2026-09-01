"""v1.1.3 D1+D3：环境检测 + Playwright Chromium 识别 + WebView2 三检。

功能：
- 识别 5 种 Playwright Chromium 可执行文件名（抖音 v1.4.2 经验）
- WebView2 64/32/WOW6432Node 三处注册表都查
- 干净机兜底：找不到时引导下载或提示路径
- 集成到 backend.app.main：startup 时调一次，浮动日志输出
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import platform
from pathlib import Path
from typing import Optional


# v1.1.3 D1：Playwright Chromium 5 种文件名兼容（抖音 v1.4.2 经验）
PLAYWRIGHT_CHROMIUM_NAMES = (
    "chrome-headless-shell.exe",      # 官方 headless shell（Windows）
    "headless_shell.exe",              # 旧版命名
    "chromium-headless-shell.exe",     # 第三方打包可能改名
    "chrome.exe",                      # 完整 chromium
    "chromium.exe",                    # 跨平台命名
)
# Linux/macOS 命名
PLAYWRIGHT_CHROMIUM_NAMES_POSIX = (
    "chrome-headless-shell",
    "headless_shell",
    "chrome",
    "chromium",
)


def get_localappdata_ms_playwright() -> Path:
    """Windows/macOS/Linux 的 Playwright 默认浏览器目录。"""
    if os.environ.get("WEISHUSHU_SELF_TEST_ROOT"):
        from backend.app.runtime_context import resolve_runtime_context

        return resolve_runtime_context().cache_root / "ms-playwright"
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA")
        if not localappdata:
            localappdata = str(Path.home() / "AppData/Local")
        base = Path(localappdata)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if not xdg_cache:
            xdg_cache = str(Path.home() / ".cache")
        base = Path(xdg_cache)
    return base / "ms-playwright"


def get_frozen_ms_playwright() -> Optional[Path]:
    """封包版内置 Playwright 浏览器目录。"""
    if not getattr(sys, "frozen", False):
        return None
    from backend.app.runtime_context import resolve_runtime_context

    ctx = resolve_runtime_context()
    if sys.platform == "darwin":
        # PyInstaller macOS .app 的 COLLECT 内容位于 sys._MEIPASS。
        meipass = ctx.resource_root / "ms-playwright"
        if meipass.exists():
            return meipass
    # PyInstaller onedir 模式：_internal 在 exe 同级目录。
    internal_root = ctx.executable_root / "_internal"
    canonical = internal_root / "ms-playwright"
    if canonical.exists():
        return canonical
    if internal_root.exists():
        try:
            has_direct_chromium = any(
                child.is_dir() and child.name.startswith(("chromium_headless_shell-", "chromium-"))
                for child in internal_root.iterdir()
            )
        except OSError:
            has_direct_chromium = False
        if has_direct_chromium:
            return internal_root
    return None


def configure_frozen_playwright_browsers_path() -> Optional[Path]:
    """让 Playwright 在 macOS `.app` 中使用随包 Chromium。"""
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        from backend.app.runtime_context import resolve_runtime_context
        archive = resolve_runtime_context().resource_root / "playwright-browsers.tar.gz"
        if archive.exists():
            from backend.app.platform_paths import ensure_dir, platform_paths

            browser_dir = platform_paths().cache_dir() / "ms-playwright"
            has_chromium = browser_dir.exists() and any(browser_dir.glob("chromium-*"))
            if not has_chromium:
                shutil.rmtree(browser_dir, ignore_errors=True)
                ensure_dir(browser_dir)
                try:
                    with tarfile.open(archive, "r:gz") as bundle:
                        bundle.extractall(browser_dir, filter="fully_trusted")
                except Exception:
                    shutil.rmtree(browser_dir, ignore_errors=True)
                    raise
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
            return browser_dir

    bundled = get_frozen_ms_playwright()
    if bundled is None:
        return None
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    return bundled


def find_chromium_executable() -> Optional[Path]:
    """v1.1.3 D1：按优先级查找 Playwright Chromium：
    1. PLAYWRIGHT_BROWSERS_PATH（macOS 本机包首次解压后的目录）
    2. 封包内置目录
    3. 当前平台的默认缓存目录
    4. None（提示用户需要下载）
    """
    names = PLAYWRIGHT_CHROMIUM_NAMES if sys.platform == "win32" else PLAYWRIGHT_CHROMIUM_NAMES_POSIX

    roots: list[Path] = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        roots.append(Path(configured))
    frozen_dir = get_frozen_ms_playwright()
    if frozen_dir:
        roots.append(frozen_dir)
    if not os.environ.get("WEISHUSHU_SELF_TEST_ROOT"):
        roots.append(get_localappdata_ms_playwright())

    for browser_root in dict.fromkeys(roots):
        if not browser_root.exists():
            continue
        for chromium_dir in browser_root.iterdir():
            if not chromium_dir.is_dir():
                continue
            for name in names:
                candidates = list(chromium_dir.rglob(name))
                if candidates:
                    return candidates[0]

    return None


# v1.1.3 D3：WebView2 64/32/WOW6432Node 三处注册表
WEBVIEW2_REG_KEYS_64 = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)
WEBVIEW2_REG_KEYS_32 = (
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)


def check_webview2_installed() -> bool:
    """v1.1.3 D3：三处注册表 + 文件系统兜底全查。"""
    if sys.platform != "win32":
        return True  # macOS/Linux pywebview 走系统 WebKit

    # 方法 1：注册表三处都查
    try:
        import winreg
        keys = WEBVIEW2_REG_KEYS_64 + WEBVIEW2_REG_KEYS_32
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key_path in keys:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        # 注册表项存在即表示安装
                        return True
                except FileNotFoundError:
                    continue
    except ImportError:
        pass

    # 方法 2：文件系统兜底
    webview2_paths = [
        Path(r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application"),
        Path(r"C:\Program Files\Microsoft\EdgeWebView\Application"),
    ]
    for p in webview2_paths:
        if p.exists() and any(p.iterdir()):
            return True

    return False


def check_chromium_ready() -> dict:
    """综合检查，返回结构化状态。"""
    return {
        "chromium_executable": str(find_chromium_executable() or ""),
        "chromium_ready": find_chromium_executable() is not None,
        "webview2_installed": check_webview2_installed(),
        "platform": sys.platform,
        "frozen": getattr(sys, "frozen", False),
    }


# ====== 启动时自检（main.py lifespan 调用）======
def install() -> dict:
    """FastAPI startup 时调一次，结果记 log_handler。"""
    import logging
    logger = logging.getLogger(__name__)
    bundled = configure_frozen_playwright_browsers_path()
    if bundled is not None:
        logger.info("✅ 使用内置 Playwright Chromium: %s", bundled)
    status = check_chromium_ready()
    if status["chromium_ready"]:
        logger.info("✅ Playwright Chromium 就绪: %s", status["chromium_executable"])
    else:
        logger.warning("⚠️ Playwright Chromium 未找到 — 扫码/Chrome 导入可能失败")
    if not status["webview2_installed"] and sys.platform == "win32":
        logger.warning("⚠️ WebView2 未安装 — pywebview 窗口可能无法启动")
    return status
