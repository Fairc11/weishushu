"""从 Chrome 浏览器导入微博登录态

流程：
1. 检测用户 Chrome 是否在跑
2. 若在跑 → 友好提示用户关掉（绝不再 taskkill /f 杀用户标签页）
3. 用 Playwright 启动**独立** Chrome 实例（隔离 user-data-dir）
4. 用户登录微博
5. 提取 cookies 保存到文件
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)


def _chrome_profile_dir() -> Path:
    """返回当前运行 profile 专用的 Chrome 导入目录。"""
    from backend.app.platform_paths import platform_paths

    return platform_paths().cache_dir() / "chrome-import-profile"


def _find_chrome() -> Optional[str]:
    """找 Chrome / Edge 可执行文件路径（v1.1.1 阶段：加 Edge 兜底）"""
    candidates = [
        # Chrome 优先
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        os.path.expanduser("~/AppData/Local/Google/Chrome/Application/chrome.exe"),
        # Edge 兜底（Win10/11 自带）
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _is_chrome_running() -> bool:
    """检测用户 Chrome 是否在跑。**不再杀进程**——只探测。"""
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/fi", "imagename eq chrome.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return "chrome.exe" in r.stdout
        # macOS / Linux
        r = subprocess.run(["pgrep", "-f", "chrome"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception as exc:
        logger.debug("检测 Chrome 进程失败: %s", exc)
        return False


def _wait_for_chrome_to_close(timeout: int = 30) -> bool:
    """友好等待：每 5s 提示一次用户关 Chrome，超时放弃。绝不 taskkill。"""
    deadline = time.time() + timeout
    poll = 5
    while time.time() < deadline:
        if not _is_chrome_running():
            return True
        remaining = int(deadline - time.time())
        logger.info(
            "⏳ 检测到 Chrome 在运行（还剩 %ds）...\n"
            "   请手动关闭 Chrome 窗口（保留工作标签页），\n"
            "   或点「忽略」继续（将启动独立实例）",
            remaining,
        )
        time.sleep(poll)
    return not _is_chrome_running()


def import_from_chrome(cookie_file: Optional[str]=None, login_timeout: int=300) -> Optional[list[dict]]:
    """从 Chrome 获取微博登录态

    v1.1.1 改造（修复 S1）：
    - **不**再 taskkill /f 杀用户 Chrome（保护用户标签页）
    - 用独立 user-data-dir 启动 Playwright，与用户 Chrome 完全隔离
    - 若用户 Chrome 在跑 → 提示手动关（30s 宽限）+ 可选忽略

    1. 启动**独立** Chrome 实例（隔离 user-data-dir）
    2. 用户登录微博
    3. 导出 cookies
    """
    from playwright.sync_api import sync_playwright
    chrome_path = _find_chrome()
    if not chrome_path:
        logger.info('❌ 未找到 Chrome / Edge 浏览器')
        return None
    logger.info('=' * 60)
    logger.info('  从 Chrome 导入微博登录态')
    logger.info('=' * 60)
    # 检测而非杀掉
    if _is_chrome_running():
        logger.info('⚠️  检测到 Chrome 正在运行')
        logger.info('   启动独立实例会失败（profile 被锁）')
        logger.info('   30s 内请手动关闭 Chrome；或点「忽略」继续（隔离实例）')
        if not _wait_for_chrome_to_close(timeout=30):
            logger.info('⏱️  超时，**不**杀用户 Chrome；改用隔离 user-data-dir 继续')
    else:
        logger.info('✅ Chrome 未运行，可直接启动')
    chrome_profile_dir = _chrome_profile_dir()
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info('正在启动隔离 Chrome 实例（user-data-dir=%s）...', chrome_profile_dir)
    try:
        with sync_playwright() as p:
            # launch_persistent_context 用隔离目录，**不会**触碰用户 Chrome profile
            browser = p.chromium.launch_persistent_context(
                str(chrome_profile_dir),
                headless=False,
                executable_path=chrome_path,
                args=['--remote-debugging-port=0'],
            )
            page = browser.new_page()
            page.goto('https://weibo.com', wait_until='domcontentloaded')
            time.sleep(3)
            body_text = page.inner_text('body')
            is_logged_in = not ('登录' in body_text[:1000] and '注册' in body_text[:1000])
            if not is_logged_in:
                logger.info('\n📱 请在打开的 Chrome 窗口中登录微博')
                logger.info(f'⏱️  等待登录（最长 {login_timeout} 秒）...\n')
                for i in range(login_timeout):
                    try:
                        text = page.inner_text('body')
                        if not ('登录' in text[:1000] and '注册' in text[:1000]):
                            is_logged_in = True
                            logger.info('✅ 检测到登录成功！')
                            break
                    except Exception as exc:
                        logger.debug('登录态轮询异常: %s', exc)
                    time.sleep(1)
            if not is_logged_in:
                logger.info('❌ 登录超时')
                browser.close()
                return None
            logger.info('  同步登录态到移动版...')
            page.goto('https://m.weibo.cn', wait_until='domcontentloaded')
            time.sleep(3)
            all_cookies = browser.cookies()
            weibo_cookies = [c for c in all_cookies if any((d in c.get('domain', '') for d in ['weibo.com', 'weibo.cn', 'sina.com.cn', 'passport.weibo']))]
            from .login import get_cookie_file_path, save_cookies

            save_path = get_cookie_file_path(cookie_file)
            # 统一 Cookie 保存入口：过滤、写盘与 ACL 收紧全部走 login 模块，
            # 避免此处复制第二套权限逻辑。
            saved_cookies = save_cookies(weibo_cookies, str(save_path))
            logger.info(f'✅ 已保存 {len(saved_cookies)} 个微博 Cookies 到 {save_path}')
            key_cookies = [c for c in saved_cookies if c.get('name') in ('SUB', 'SUBP', 'XSRF-TOKEN')]
            for c in key_cookies:
                logger.info(f"  🔑 {c['name']:12s} domain={c['domain']:20s}")
            browser.close()
            return weibo_cookies
    except PermissionError as exc:
        # Windows：隔离 profile 目录被上次实例或杀软占用时，原始
        # PermissionError 会直穿调用方；这里统一转成中文业务错误。
        raise WeiboError(
            "Chrome 导入失败：隔离浏览器目录被占用，请关闭 Chrome 后重试",
            kind=WeiboErrorKind.API,
            recoverable=True,
            original=exc,
        ) from exc

def quick_test(cookie_file: Optional[str]=None) -> bool:
    """快速测试 cookie 是否有效"""
    from .login import load_cookies, check_cookies_valid
    data = load_cookies(cookie_file)
    if not data:
        return False
    cl = data.get('cookies', []) if isinstance(data, dict) else data
    if not cl:
        return False
    return check_cookies_valid(cl)
