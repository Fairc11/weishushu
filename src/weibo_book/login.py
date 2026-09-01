"""微博书 - 扫码登录模块

使用 Playwright 打开微博登录页，显示二维码让用户扫码，
登录成功后提取 Cookies 并持久化到本地文件。
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional
import crawl4weibo
from playwright.sync_api import sync_playwright

from .errors import WeiboError, WeiboErrorKind
logger = logging.getLogger(__name__)
DEFAULT_COOKIE_FILE = Path.home() / '.weibo_book_cookies'
WEIBO_DOMAIN_ROOTS = ('weibo.com', 'weibo.cn', 'sina.com.cn', 'sina.cn')


def _is_supported_cookie_domain(domain: str) -> bool:
    normalized = (domain or '').strip().lstrip('.').rstrip('.').lower()
    return any(
        normalized == root or normalized.endswith(f'.{root}')
        for root in WEIBO_DOMAIN_ROOTS
    )

def get_cookie_file_path(cookie_file: Optional[str]=None) -> Path:
    if cookie_file:
        return Path(cookie_file)
    from backend.app.profile import default_cookie_filename

    return Path.home() / default_cookie_filename()

def load_cookies(cookie_file: Optional[str]=None) -> dict:
    path = get_cookie_file_path(cookie_file)
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                return {'cookies': data}
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _current_windows_account() -> Optional[str]:
    """返回 icacls 可接受的完整当前账户标识（SAM 兼容 DOMAIN\\USER）。

    不能假设 USERNAME 单独可用：微软账户/域环境下 icacls 需要带域前缀。
    """
    import os

    try:
        import win32api

        name = win32api.GetUserNameEx(win32api.NameSamCompatible)
        if name:
            return str(name)
    except Exception:
        pass
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME")
    if domain and user:
        return f"{domain}\\{user}"
    if user:
        return user
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(257)
        size = __import__("ctypes").wintypes.DWORD(257)
        if ctypes.windll.advapi32.GetUserNameW(buffer, __import__("ctypes").byref(size)):
            value = buffer.value
            if value:
                return value
    except Exception:
        pass
    return None


def _run_icacls(path: Path, args: list[str]) -> "tuple[int, str, str]":
    """执行 icacls 并返回 (returncode, stdout, stderr)；调用方必须检查。"""
    import subprocess

    completed = subprocess.run(
        ["icacls", str(path), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _restrict_file_permissions_windows(path: Path) -> None:
    """Windows ACL 收紧：先显式授权当前用户，再移除继承。

    顺序保证任何一步失败时文件仍对当前进程可读；最终校验仍不可读时
    删除文件并抛出中文业务错误，绝不留下锁死当前进程的 Cookie 文件。
    """
    principal = _current_windows_account()
    grant_rc, grant_out, grant_err = (1, "", "")
    if principal:
        grant_rc, grant_out, grant_err = _run_icacls(
            path, ["/grant", f"{principal}:F"]
        )
    if grant_rc != 0:
        logger.warning(
            "跳过 Cookie 文件 ACL 收紧（无法为 %s 授权）: rc=%s stdout=%s stderr=%s",
            principal or "<未知账户>",
            grant_rc,
            grant_out.strip(),
            grant_err.strip(),
        )
        return
    inherit_rc, inherit_out, inherit_err = _run_icacls(path, ["/inheritance:r"])
    if inherit_rc != 0:
        logger.warning(
            "Cookie 文件移除继承失败（显式授权仍在，文件保持可读）: "
            "rc=%s stdout=%s stderr=%s",
            inherit_rc,
            inherit_out.strip(),
            inherit_err.strip(),
        )
        return
    if not _file_readable_by_current_process(path):
        # 修复一次：重新显式授权后再验证。
        repair_rc, repair_out, repair_err = _run_icacls(
            path, ["/grant", f"{principal}:F"]
        )
        if repair_rc == 0 and _file_readable_by_current_process(path):
            return
        try:
            path.unlink()
        except OSError:
            pass
        raise WeiboError(
            "Cookie 文件权限收紧后当前账户失去读取权限，已删除该文件，请重新登录",
            kind=WeiboErrorKind.UNKNOWN,
        )


def _file_readable_by_current_process(path: Path) -> bool:
    try:
        with open(path, "rb"):
            pass
        return True
    except OSError:
        return False


def _restrict_file_permissions(path: Path) -> None:
    """限制文件为当前用户私有，避免 cookie 被同机其他账户读取。

    - Windows: icacls 先给当前用户 :F（完全控制），再移除继承；
      每次调用的 returncode/stdout/stderr 都必须检查
    - macOS / Linux: chmod 0o600
    """
    import os
    import sys

    if sys.platform == "win32":
        _restrict_file_permissions_windows(path)
        return
    os.chmod(path, 0o600)


def save_cookies(cookies: list[dict], cookie_file: Optional[str]=None):
    path = get_cookie_file_path(cookie_file)
    useful = []
    seen = set()
    for c in cookies:
        key = (c.get('name', ''), c.get('domain', ''), c.get('path', '/'))
        if key not in seen and _is_supported_cookie_domain(c.get('domain', '')):
            seen.add(key)
            record = {
                'name': c['name'],
                'value': c['value'],
                'domain': c.get('domain', ''),
                'path': c.get('path', '/'),
            }
            for attr in ('expires', 'secure', 'httpOnly', 'sameSite'):
                if attr in c and c[attr] is not None:
                    record[attr] = c[attr]
            useful.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(useful, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        _restrict_file_permissions(temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    logger.info('✅ Cookies 已保存 (%d 个)', len(useful))
    return useful

def cookies_to_header(cookies_list: list[dict]) -> str:
    return '; '.join((f"{c['name']}={c['value']}" for c in cookies_list))


def cookies_to_header_for_host(cookies_list: list[dict], host: str) -> str:
    """按浏览器的 Domain 匹配规则为指定主机构建 Cookie 请求头。"""
    normalized_host = (host or '').strip().rstrip('.').lower()
    matched = []
    for cookie in cookies_list:
        domain = (cookie.get('domain') or '').strip().lstrip('.').rstrip('.').lower()
        if not domain:
            matched.append(cookie)
            continue
        if normalized_host == domain or normalized_host.endswith(f'.{domain}'):
            matched.append(cookie)
    return cookies_to_header(matched)

def check_cookies_valid(cookies_data) -> bool:
    """v1.1.1 S3：改用 httpx（与其他模块一致）"""
    import httpx
    try:
        if isinstance(cookies_data, list):
            cookie_str = cookies_to_header_for_host(cookies_data, 'm.weibo.cn')
        elif isinstance(cookies_data, dict):
            cl = cookies_data.get('cookies', [])
            cookie_str = cookies_to_header_for_host(cl, 'm.weibo.cn') if cl else ''
            if not cookie_str:
                cookie_str = '; '.join((f'{k}={v}' for k, v in cookies_data.items()))
        else:
            cookie_str = str(cookies_data)
        if not cookie_str:
            return False
        resp = httpx.get('https://m.weibo.cn/api/config', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Cookie': cookie_str}, timeout=10)
        data = resp.json()
        return data.get('data', {}).get('login', False)
    except Exception:
        return False

def login_with_qrcode(cookie_file: Optional[str]=None, login_timeout: int=180, headless: bool=False) -> list[dict]:
    """
    通过微博扫码登录获取 Cookies

    采用多方案尝试：
    1. 主方案: weibo.com 登录页 → 切换到扫码登录 tab
    2. 备选: 移动端 passport 登录页

    Returns:
        list[dict]: Cookies 列表
    """
    logger.info('=' * 60)
    logger.info('  微博扫码登录')
    logger.info('=' * 60)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled'])
        context = browser.new_context(viewport={'width': 1280, 'height': 800}, user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', locale='zh-CN')
        page = context.new_page()
        page.on('console', lambda msg: None)
        logger.info('正在打开微博登录页面（扫码登录）...')
        try:
            page.goto('https://weibo.com/login', wait_until='domcontentloaded', timeout=20000)
            time.sleep(3)
            logger.info(f'  URL: {page.url[:80]}')
            if '/visitor/visitor' in page.url:
                logger.info('  检测到微博访客拦截页，切换到移动端扫码登录页')
                page.goto('https://passport.weibo.cn/signin/login', wait_until='domcontentloaded', timeout=15000)
                time.sleep(3)
                logger.info(f'  备用 URL: {page.url[:80]}')
        except Exception as e:
            logger.info(f'  登录页加载异常: {e}')
            try:
                page.goto('https://passport.weibo.cn/signin/login', wait_until='domcontentloaded', timeout=15000)
                time.sleep(3)
            except Exception as e2:
                logger.info(f'  备用页面也失败: {e2}')
        logger.info('\n📱 请在打开的浏览器窗口中，使用微博 App 扫描二维码登录')
        logger.info(f'⏱️  等待中（最长 {login_timeout} 秒）...\n')
        login_success = False
        start_time = time.time()
        now = time.time()
        last_validation_at = now - 3
        last_url = page.url
        while now - start_time < login_timeout:
            login_hint = False
            current_url = page.url
            current_url_lower = current_url.lower()
            if current_url != last_url:
                last_url = current_url
                if any((pat in current_url_lower for pat in ['/home', '/u/', 'my', 'profile', 'index'])) and 'login' not in current_url_lower:
                    login_hint = True
            try:
                for sel in ['[node-type="wrapper"]', '.gn_nav', '.WB_frame', '[class*="avatar"]', "[class*='logined']", '.name']:
                    if page.query_selector(sel):
                        login_hint = True
                        break
            except Exception as exc:
                import logging
                logging.getLogger(__name__).debug('登录状态轮询异常: %s', exc)
            try:
                current_cookies = context.cookies()
                for c in current_cookies:
                    if c.get('name') in ('SUB', 'SSOLoginState') and c.get('value'):
                        login_hint = True
                        break
                if login_hint and now - last_validation_at >= 3:
                    last_validation_at = now
                    if check_cookies_valid(current_cookies):
                        login_success = True
                        logger.info('  登录凭证已通过微博校验')
                    else:
                        logger.debug('检测到 Cookie，但微博仍判定为未登录')
                if login_success:
                    break
            except Exception as exc:
                import logging
                logging.getLogger(__name__).debug('Cookie 检测异常: %s', exc)
            time.sleep(1)
            now = time.time()
        if not login_success:
            logger.info('❌ 登录超时，未能完成登录')
            browser.close()
            return []
        logger.info('✅ 登录成功！正在提取 Cookies...')
        time.sleep(1)
        all_cookies = context.cookies()
        browser.close()
        result = save_cookies(all_cookies, cookie_file)
        key_cookies = [c for c in all_cookies if c.get('name') in ('SUB', 'SUBP', 'SCF', 'SSOLoginState')]
        if key_cookies:
            logger.info(f'🔑 已获取 {len(key_cookies)} 个关键认证 Cookie')
        return result

def validate_stored_cookies(cookie_file: Optional[str]=None) -> bool:
    data = load_cookies(cookie_file)
    if isinstance(data, dict) and 'cookies' in data:
        return check_cookies_valid(data['cookies'])
    return check_cookies_valid(data)
