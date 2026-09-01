# -*- mode: python ; coding: utf-8 -*-
"""微书薯 macOS arm64 本机 `.app` 配置。

通过环境变量 `WEISHUSHU_PROFILE=dev` 切换开发版配置：
- Bundle ID：`com.weishushu.desktop.dev`（用户版 `com.weishushu.desktop`）
- 显示名：`微书薯 Dev`（用户版 `微书薯`）
- bundle / 可执行文件名：`WeishushuDev`（用户版 `Weishushu`）
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


if sys.platform != "darwin":
    raise SystemExit("build_mac.spec 只能在 macOS 上执行")


ROOT = Path.cwd().resolve()


def _profile() -> str:
    """返回当前 profile（'dev' 或 ''）。"""
    return os.environ.get("WEISHUSHU_PROFILE", "").strip().lower()


def _is_dev() -> bool:
    return _profile() == "dev"


BUNDLE_NAME = "WeishushuDev" if _is_dev() else "Weishushu"
BUNDLE_IDENTIFIER = "com.weishushu.desktop.dev" if _is_dev() else "com.weishushu.desktop"
BUNDLE_DISPLAY_NAME = "微书薯 Dev" if _is_dev() else "微书薯"
datas = [
    (str(ROOT / "backend" / "app" / "templates"), "backend/app/templates"),
    (str(ROOT / "backend" / "app" / "static"), "backend/app/static"),
    (str(ROOT / "weibo_book" / "templates"), "weibo_book/templates"),
    (str(ROOT / "desktop" / "self_test" / "fixtures"), "desktop/self_test/fixtures"),
]
datas += copy_metadata("crawl4weibo")


def _browser_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        roots.append(Path(configured))
    roots.extend([
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / ".cache" / "ms-playwright",
    ])
    return list(dict.fromkeys(roots))


browser_root: Path | None = None
for browser_root in _browser_roots():
    if not browser_root.exists():
        continue
    if any(child.is_dir() and child.name.startswith(("chromium-", "chromium_headless_shell-", "ffmpeg-"))
           for child in browser_root.iterdir()):
        break
else:
    browser_root = None

if browser_root is None:
    raise SystemExit("缺少 Playwright Chromium；请先运行 scripts/build_mac.sh")

# Chromium 含嵌套 `.app`。若直接放进 datas，PyInstaller 会把其中 Mach-O 自动重分类为
# BINARY 并试图重签名，导致封包失败。归档作为纯数据保留权限，首次运行再解压到用户缓存。
browser_archive = Path(shutil.make_archive(
    str(ROOT / "build" / "playwright-browsers"),
    "gztar",
    root_dir=browser_root,
))
datas.append((str(browser_archive), "."))


hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "webview",
    "webview.platforms.cocoa",
    "desktop.browser.policy",
    "desktop.browser.state",
    "desktop.browser.mac_webkit",
    "AppKit",
    "Foundation",
    "WebKit",
    "PyObjCTools",
    "crawl4weibo",
    "crawl4weibo.models",
    "crawl4weibo.models.post",
    "crawl4weibo.models.comment",
    "weibo_book",
    "weibo_book.api",
    "weibo_book.errors",
    "weibo_book.extractor",
    "weibo_book.generator",
    "weibo_book.media",
    "weibo_book.login",
    "weibo_book.chrome_import",
    "weibo_book.models",
    "weibo_book.reports",
    "weibo_book.cli",
]


a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(Path.cwd().resolve() / "packaging/pyinstaller/runtime_hook.py")],
    excludes=["pytest", "_pytest", "tests", "docs", "win32", "pythoncom", "comtypes"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=BUNDLE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BUNDLE_NAME,
)
app = BUNDLE(
    coll,
    name=f"{BUNDLE_NAME}.app",
    icon="icon.icns",
    bundle_identifier=BUNDLE_IDENTIFIER,
    info_plist={
        "CFBundleName": BUNDLE_NAME,
        "CFBundleDisplayName": BUNDLE_DISPLAY_NAME,
        "CFBundleShortVersionString": "2.0.1",
        "CFBundleVersion": "2.0.1",
        "CFBundleDevelopmentRegion": "zh-Hans",
        "CFBundleLocalizations": ["zh-Hans"],
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
