"""应用 profile：区分开发版与用户版，避免数据污染和视觉混淆。

源码开发态通过环境变量 `WEISHUSHU_PROFILE=dev` 切换开发版配置；
frozen 开发版根据 PyInstaller 锁定的可执行文件名 `WeishushuDev` 自行识别：
- Bundle ID：`com.weishushu.desktop.dev`（用户版 `com.weishushu.desktop`）
- 显示名：`微书薯 Dev`（用户版 `微书薯`）
- 数据目录：`WeishushuDev`（用户版 `Weishushu`）
- Cookie 文件：`.weibo_book_cookies_dev`（用户版 `.weibo_book_cookies`）
- 窗口标题：`微书薯 Dev（开发版）`（用户版 `微书薯`）

用户版构建零变化（可执行文件名 `Weishushu`）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROFILE_ENV = "WEISHUSHU_PROFILE"
DEV_PROFILE = "dev"
DEV_EXECUTABLE_NAME = "WeishushuDev"


def _frozen_executable_profile() -> str:
    """从 frozen 产物的精确可执行文件名识别开发版。"""
    if not getattr(sys, "frozen", False):
        return ""
    executable_name = Path(sys.executable).name
    return DEV_PROFILE if executable_name == DEV_EXECUTABLE_NAME else ""


def configure_source_profile() -> None:
    """源码入口默认启用开发 profile，避免读取日常个人版登录态。"""
    if not getattr(sys, "frozen", False):
        os.environ[PROFILE_ENV] = DEV_PROFILE


def app_profile() -> str:
    """返回当前 profile；frozen 只接受精确可执行文件名。"""
    if getattr(sys, "frozen", False):
        return _frozen_executable_profile()
    configured = os.environ.get(PROFILE_ENV, "").strip().lower()
    if configured == DEV_PROFILE:
        return DEV_PROFILE
    return ""


def is_dev_profile() -> bool:
    """是否为开发版 profile。"""
    return app_profile() == DEV_PROFILE


def app_dirname() -> str:
    """应用数据目录名（用户版 `Weishushu` / 开发版 `WeishushuDev`）。"""
    return "WeishushuDev" if is_dev_profile() else "Weishushu"


def app_bundle_name() -> str:
    """`.app` bundle 名称（不含 `.app` 后缀）。"""
    return "WeishushuDev" if is_dev_profile() else "Weishushu"


def app_executable_name() -> str:
    """可执行文件名。"""
    return "WeishushuDev" if is_dev_profile() else "Weishushu"


def app_display_name() -> str:
    """用户可见显示名。"""
    return "微书薯 Dev" if is_dev_profile() else "微书薯"


def window_title() -> str:
    """窗口标题（开发版加「（开发版）」后缀便于区分）。"""
    base = app_display_name()
    return f"{base}（开发版）" if is_dev_profile() else base


def default_cookie_filename() -> str:
    """默认 Cookie 文件名（开发版独立文件避免污染用户版登录态）。"""
    return ".weibo_book_cookies_dev" if is_dev_profile() else ".weibo_book_cookies"


def bundle_identifier() -> str:
    """macOS Bundle ID（开发版独立 ID 避免与用户版混淆）。"""
    return "com.weishushu.desktop.dev" if is_dev_profile() else "com.weishushu.desktop"


def dmg_output_name(version: str) -> str:
    """DMG 输出文件名。"""
    suffix = "Dev" if is_dev_profile() else ""
    return f"Weishushu{suffix}-v{version}-macOS-arm64.dmg"
