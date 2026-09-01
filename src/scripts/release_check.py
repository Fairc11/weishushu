"""v2.0.0 release_check.py — 33 项硬约束（B18: 27 → 33）。

参考抖音 v1.4.2 release_check.py（16→20 项）。在 build_exe.bat 阶段 3 跑，
任何一项 FAIL 必须修复后重跑，杜绝"开发机过了干净机跪"。

返回：0 = 全部 PASSED，可打包；非 0 = 有 FAIL，禁止打包。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import re
import sys
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

# Windows / GBK terminal fix：cmd 默认 GBK 编码下 print(✅/❌) 会直接 UnicodeEncodeError 崩
# （v1.1.6 P1-6 复现：'gbk' codec can't encode character '✅'）
# 参考 weibo_book/__init__.py 同样的兜底
if sys.stdout is not None:
    try:
        enc = sys.stdout.encoding
        if enc and enc.lower() not in ("utf-8", "utf8") and sys.stdout.buffer is not None:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
            )
            if sys.stderr is not None and sys.stderr.buffer is not None:
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer,
                    encoding="utf-8",
                    errors="replace",
                )
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]

# W1不再单独运行Run tests；release_check必须自身真实运行完整pytest。
# Windows完整pytest实际约235秒，旧180秒硬编码会误超时，这里放宽到600秒。
PYTEST_TIMEOUT_SECONDS = 600

THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS = (
    "backend.app.profile",
    "WEISHUSHU_PROFILE",
    "WeishushuDev",
    ".weibo_book_cookies_dev",
    "defaultDataStore()",
    "nonPersistentDataStore()",
    "不得回退",
    "frozen 只信任可执行文件名",
    "get_cookie_file_path()",
    "chrome-import-profile",
    "旧 Windows Cookie",
    "冲突参数",
)


def _without_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _markdown_section(text: str, heading: str) -> str:
    visible = _without_html_comments(text)
    start = visible.find(heading)
    if start < 0:
        return ""
    following_heading = re.search(r"^##\s+", visible[start + len(heading):], re.MULTILINE)
    if following_heading is None:
        return visible[start:]
    end = start + len(heading) + following_heading.start()
    return visible[start:end]


def _clinerules_isolation_rule(text: str) -> str:
    visible = _without_html_comments(text)
    marker = re.search(
        r"^12\.\s+.*不(?:得)?突破三端隔离.*$",
        visible,
        flags=re.MULTILINE,
    )
    if marker is None:
        return ""
    following_rule = re.search(r"^\d+\.\s+", visible[marker.end():], re.MULTILINE)
    if following_rule is None:
        return visible[marker.start():]
    end = marker.end() + following_rule.start()
    return visible[marker.start():end]

STANDARD_MIT_LICENSE = """MIT License

Copyright (c) 2026 Weishushu contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

REVIEWED_WINDOWS_WEBVIEW2_V147_DECISION = """# Windows WebView2 v147 技术决策

## 复现背景

Windows 历史路线 A 试图使用 Win32 和 WebView2 双控件，在主窗口内渲染网页。该路线假设可以从已安装的 WebView2 Runtime 目录直接加载 `WebView2Loader.dll`，再通过简单 C 导出调用 `CreateCoreWebView2EnvironmentWithOptions`。

## 精确证据

- Windows 环境已检测到 WebView2 Runtime `147.0.3912.86`。
- Win32 离屏窗口可以创建和销毁。
- 该运行时路径中不存在旧路线假设的 `WebView2Loader.dll`。
- `CreateCoreWebView2EnvironmentWithOptions 不再作为 C 导出`，因此旧路线所需的调用路径不可用。

## 影响

以上证据足以确认，依赖 `comtypes` 与简单 C 导出的旧路线 A 对 v147 不可行。该结论不等于 WebView2 原生宿主永久不可行；使用正式 SDK、.NET 包装或独立原生宿主需要另行设计和验证。

## 决策

- 旧路线 A 不实施，不进入当前主线。
- Windows 链路仅保留历史兼容策略：pywebview 的 WebView2 支持、WebView2 Runtime 注册表检测，以及 `C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application` 与 `C:\\Program Files\\Microsoft\\EdgeWebView\\Application` 文件系统兜底检测。"""


class Check(NamedTuple):
    name: str
    ok: bool
    msg: str
    fatal: bool = True


def check(name: str, ok: bool, msg: str = "", fatal: bool = True) -> Check:
    return Check(name, ok, msg, fatal)


def _assigned_string_literals(source: str, assignment_name: str) -> set[str]:
    """读取指定顶层赋值中的精确字符串，不执行被检查脚本。"""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == assignment_name
            for target in node.targets
        ):
            continue
        values = node.value
        if isinstance(values, ast.Call) and len(values.args) == 1:
            values = values.args[0]
        if not isinstance(values, (ast.Set, ast.Tuple, ast.List)):
            return set()
        return {
            item.value
            for item in values.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
    return set()


# ====== 业务代码红线 ======

def check_01_no_print_in_business() -> Check:
    """1. 业务代码 weibo_book/ 0 个 print() 残留（v1.1.1 A2）"""
    bad = []
    for f in (ROOT / "weibo_book").glob("*.py"):
        if f.name in ("cli.py", "__main__.py"):
            continue  # CLI 入口允许 print
        src = f.read_text(encoding="utf-8")
        # 简单计数：行首 print( 或 \nprint(
        for n, line in enumerate(src.splitlines(), 1):
            if re.match(r'^\s*print\s*\(', line):
                bad.append(f"{f.name}:{n}")
    return check("01 业务代码无 print", len(bad) == 0,
                 f"{len(bad)} 个残留" if bad else "0 残留")


def check_02_chinese_error_keywords() -> Check:
    """2. classify_error 含中文关键字（v1.1.1 L4）"""
    f = ROOT / "weibo_book" / "errors.py"
    if not f.exists():
        return check("02 中文错误关键字", False, "errors.py 缺失")
    src = f.read_text(encoding="utf-8")
    keywords = ["超时", "网络", "断网", "重置"]
    missing = [k for k in keywords if k not in src]
    return check("02 中文错误关键字", len(missing) == 0,
                 f"缺 {missing}" if missing else f"全在（{len(keywords)}/{len(keywords)}）")


def check_03_no_taskkill_in_chrome() -> Check:
    """3. chrome_import.py 不再有 taskkill /f（v1.1.1 S1）"""
    f = ROOT / "weibo_book" / "chrome_import.py"
    src = f.read_text(encoding="utf-8")
    # 排除 docstring / 注释
    tree = ast.parse(src)
    has_real = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "subprocess" and node.args:
                if isinstance(node.args[0], ast.Constant) and "taskkill" in str(node.args[0].value):
                    has_real = True
    return check("03 chrome 无 taskkill", not has_real, "taskkill 不在" if not has_real else "taskkill 还在真代码")


def check_04_cookie_permissions() -> Check:
    """4. login.py 有 cookie 权限收紧（v1.1.1 S2）"""
    f = ROOT / "weibo_book" / "login.py"
    src = f.read_text(encoding="utf-8")
    has = "_restrict_file_permissions" in src and "icacls" in src and "chmod" in src
    return check("04 cookie 权限收紧", has, "_restrict_file_permissions 在" if has else "_restrict_file_permissions 缺")


def check_05_partial_marker() -> Check:
    """5. WeiboExtractor 有 partial 4 字段（v1.1.1 L1）"""
    f = ROOT / "weibo_book" / "extractor.py"
    src = f.read_text(encoding="utf-8")
    fields = ["_last_partial", "_last_pages_failed", "_last_pages_total", "_last_partial_reason"]
    missing = [fld for fld in fields if fld not in src]
    return check("05 partial 4 字段", len(missing) == 0,
                 f"缺 {missing}" if missing else "全在")


# ====== 包装层 ======

def check_06_8_routers() -> Check:
    """6. backend/app/routers/ 8 router 全在"""
    routers_dir = ROOT / "backend" / "app" / "routers"
    required = ["router_profile", "router_scraper", "router_login",
                "router_download", "router_tasks", "router_logs",
                "router_assets", "router_ws", "router_favorites"]  # v1.1.3 +1
    missing = [r for r in required if not (routers_dir / f"{r}.py").exists()]
    return check("06 8+1 router 全在", len(missing) == 0,
                 f"缺 {missing}" if missing else "9 router")


def check_07_main_includes_all_routers() -> Check:
    """7. main.py 9 router 全 include_router"""
    f = ROOT / "backend" / "app" / "main.py"
    src = f.read_text(encoding="utf-8")
    routers = ["profile_router", "scraper_router", "login_router", "download_router",
               "tasks_router", "logs_router", "assets_router", "favorites_router"]
    has_loop = "include_router(r)" in src or "include_router(r," in src
    has_individual = all(f"include_router({r}" in src for r in routers)
    missing = [] if (has_loop or has_individual) else ["无 include_router"]
    return check("07 main.py include 9 router", len(missing) == 0,
                 f"全 include（loop={has_loop}）" if not missing else f"缺 {missing}")


def check_08_ws_router_first() -> Check:
    """8. WS router 优先 include（在 catch-all 之前）"""
    f = ROOT / "backend" / "app" / "main.py"
    src = f.read_text(encoding="utf-8")
    ws_idx = src.find("ws_router")
    # 既支持 include_router(profile_router) 也支持 for-loop include_router(r) 后接 profile_router 引用
    other_idx = -1
    for name in ("profile", "scraper", "login", "favorites", "assets"):
        # 任意方式：include_router 引用 / from 引用
        for pat in (f"include_router({name}_router)", f"as {name}_router"):
            idx = src.find(pat)
            if idx > 0 and (other_idx < 0 or idx < other_idx):
                other_idx = idx
    ok = ws_idx > 0 and other_idx > 0 and ws_idx < other_idx
    return check("08 WS router 优先", ok, f"ws@{ws_idx} other@{other_idx}")


def check_09_rate_limit() -> Check:
    """9. 3 端点 rate limit（v1.1.1 S4）"""
    f = ROOT / "backend" / "app" / "services" / "rate_limit.py"
    if not f.exists():
        return check("09 rate_limit.py 存在", False, "缺文件")
    src = f.read_text(encoding="utf-8")
    has_check = all(name in src for name in ("check_scraper", "check_login", "check_profile"))
    return check("09 rate_limit 3 端点", has_check, "3 端点全在" if has_check else "缺 check_scraper/login/profile")


# ====== 前端 ======

def _frontend_module_source() -> str:
    module_root = ROOT / "backend" / "app" / "static" / "js" / "modules"
    names = ("state.js", "feedback.js", "login.js", "archive.js", "tasks.js", "desktop.js")
    return "\n".join((module_root / name).read_text(encoding="utf-8") for name in names)

def check_10_no_mode_toggle() -> Check:
    """10. 前端无 mode-toggle DOM 元素（v1.1.2 F1 砍 no_login）

    允许代码注释提到"mode-toggle"，禁止的是 DOM 元素。
    """
    src = _frontend_module_source()
    real_usage = bool(
        re.search(r"getElementById\(['\"]mode-toggle", src)
        or re.search(r'class="mode-toggle"', src)
        or re.search(r"querySelector\(['\"]mode-toggle", src)
    )
    return check("10 前端无 mode-toggle DOM", not real_usage,
                 "DOM 无 mode-toggle" if not real_usage else "还残留 DOM")


def check_11_floating_log() -> Check:
    """11. 浮动日志面板（log-toggle / log-panel）"""
    src = _frontend_module_source()
    return check("11 浮动日志", "log-toggle" in src and "log-panel" in src, "log-toggle+log-panel 在" if "log-toggle" in src else "缺")


# ====== 打包 ======

def check_12_build_spec_exists() -> Check:
    """12. 按当前平台检查打包 spec。"""
    name = "build_mac.spec" if sys.platform == "darwin" else "build.spec"
    f = ROOT / name
    return check(f"12 {name} 存在", f.exists() and f.stat().st_size > 100)


def check_13_installer_iss_exists() -> Check:
    """13. installer.iss 存在"""
    if sys.platform == "darwin":
        return check("13 macOS 跳过 Inno Setup", True, "本机 .app 不使用 installer.iss")
    f = ROOT / "installer.iss"
    if not f.exists():
        return check("13 installer.iss 存在", False, "缺文件")
    src = f.read_text(encoding="utf-8")
    has_cleanup = "CurUninstallStepChanged" in src and "DelTree" in src
    return check("13 installer.iss + 卸载清理", has_cleanup, "CurUninstallStepChanged 在" if has_cleanup else "缺卸载清理 [Code] 段")


def check_14_build_bat_exists() -> Check:
    """14. build_exe.bat 存在"""
    if sys.platform == "darwin":
        f = ROOT / "scripts" / "build_mac.sh"
        return check("14 scripts/build_mac.sh 存在", f.exists() and f.stat().st_size > 100)
    f = ROOT / "build_exe.bat"
    return check("14 build_exe.bat 存在", f.exists() and f.stat().st_size > 100)


def check_15_playwright_5_filenames() -> Check:
    """15. setup_check.py 含 5 文件名（v1.1.3 D1）"""
    f = ROOT / "backend" / "app" / "services" / "setup_check.py"
    if not f.exists():
        return check("15 Playwright 5 文件名", False, "缺 setup_check.py")
    src = f.read_text(encoding="utf-8")
    names = ["chrome-headless-shell.exe", "headless_shell.exe", "chromium-headless-shell.exe", "chrome.exe", "chromium.exe"]
    missing = [n for n in names if n not in src]
    return check("15 5 文件名", len(missing) == 0, f"缺 {missing}" if missing else "全在")


# ====== 文档 ======

def check_16_changelog_v114() -> Check:
    """16. CHANGELOG.md 含当前 v2.0.0 + 上一公开历史 v1.1.6 两节。"""
    f = ROOT / "CHANGELOG.md"
    if not f.exists():
        return check("16 CHANGELOG.md", False, "缺")
    text = f.read_text(encoding="utf-8")
    has_200 = "[2.0.0]" in text
    has_116 = "[1.1.6]" in text
    if has_200 and has_116:
        return check("16 CHANGELOG 含 v2.0.0 + v1.1.6", True, "两节均在")
    missing = []
    if not has_200:
        missing.append("v2.0.0")
    if not has_116:
        missing.append("v1.1.6")
    return check("16 CHANGELOG 含 v2.0.0 + v1.1.6", False, f"缺: {', '.join(missing)}")


def check_17_root_agents_md() -> Check:
    """17. 项目根 AGENTS.md 存在且包含三端隔离硬规则。"""
    f = ROOT / "AGENTS.md"
    if not f.exists() or f.stat().st_size <= 1000:
        return check("17 AGENTS.md 三端隔离规则", False, "不存在或太小")
    text = _markdown_section(
        f.read_text(encoding="utf-8"),
        "## 三端隔离硬规则",
    )
    if not text:
        return check(
            "17 AGENTS.md 三端隔离规则",
            False,
            f"缺 {['三端隔离硬规则', *THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS]}",
        )
    missing = [
        token
        for token in THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS
        if token not in text
    ]
    return check(
        "17 AGENTS.md 三端隔离规则",
        not missing,
        f"缺 {missing}" if missing else "完整",
    )


def check_18_no_relative_time() -> Check:
    """18. 文档无残留相对时间（"今天/昨天/最近"）"""
    md_files = list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md"))
    bad = []
    for f in md_files:
        # 规范/规则类文档允许引用"今天"作为反例
        if "docs-convention" in f.name or "user_ptu" in f.name or "user_visual" in f.name:
            continue
        if "DOCS_TODO" in f.name or "CONVENTION" in f.name.upper():
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            # 排除产品功能描述："看最近 20 条微博"
            if re.search(r"看最近 \d+ 条|最近 \d+ 天|看最近 7 天", line):
                continue
            # 命中"今天"或"昨天"
            if "今天" in line or "昨天" in line:
                bad.append(f"{f.name}:{n}: {line[:50]}")
    return check("18 文档无相对时间", len(bad) == 0, f"{len(bad)} 处" if bad else "全用绝对时间")


# ====== 测试 ======

def check_19_tests_pass() -> Check:
    """19. 测试全绿（W1只运行一次完整pytest，由本项真实执行）。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            return check("19 测试全绿", False, f"pytest exit={r.returncode}: {out[-200:]}")
        m = re.search(r"(\d+) passed", out)
        if m and int(m.group(1)) >= 199:
            return check("19 测试全绿", True, f"{m.group(1)} passed")
        return check("19 测试全绿", False, f"out={out[-200:]}")
    except subprocess.TimeoutExpired as exc:
        return check(
            "19 测试全绿",
            False,
            f"完整pytest超时（{PYTEST_TIMEOUT_SECONDS}秒）: {exc}",
        )
    except Exception as e:
        return check("19 测试", False, str(e)[:80])


def check_20_compileall() -> Check:
    """20. 字节码编译全过"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "compileall", "-q",
             "backend", "weibo_book", "tests", "desktop_app.py", "js_api.py", "run.py"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return check("20 字节码编译", True, "全过")
        return check("20 字节码编译", False, r.stderr[-200:] or r.stdout[-200:])
    except Exception as e:
        return check("20 字节码编译", False, str(e)[:80])


# ====== v1.2.0 V120-6: 红线扫描（10 不做）======

def check_21_risks_md_exists() -> Check:
    """21. RISKS.md 存在（v1.2.0 起强制，8 类风险 + 10 不做）"""
    p = ROOT / "RISKS.md"
    if p.exists() and p.stat().st_size > 1000:
        return check("21 RISKS.md 存在", True, f"{p.stat().st_size} bytes")
    return check("21 RISKS.md 存在", False, f"不存在或太小: {p}")


def check_22_clinerules_exists() -> Check:
    """22. .clinerules 存在并包含三端隔离硬规则。"""
    p = ROOT / ".clinerules"
    if not p.exists() or p.stat().st_size <= 200:
        return check("22 .clinerules 三端隔离规则", False, "不存在或太小")
    text = _clinerules_isolation_rule(p.read_text(encoding="utf-8"))
    if not text:
        return check(
            "22 .clinerules 三端隔离规则",
            False,
            f"缺 {['不突破三端隔离', *THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS]}",
        )
    missing = [
        token
        for token in THREE_ENVIRONMENT_ISOLATION_RULE_TOKENS
        if token not in text
    ]
    return check(
        "22 .clinerules 三端隔离规则",
        not missing,
        f"缺 {missing}" if missing else "完整",
    )


def check_23_no_redline_keywords_in_business() -> Check:
    """23. 业务代码无红线关键词（v1.2.0 红线 10 不做扫描）

    检测 weibo_book/ + backend/app/{routers,services}/ 中是否含：
    - 多账号 / 代理池 / Cookie 池 / 账号包
    - 验证码绕过 / OAuth 商业用途
    """
    redline_keywords = [
        "多账号", "代理池", "Cookie 池", "账号包",
        "验证码绕过", "OAuth 商业用途",
    ]
    scanned_dirs = [
        ROOT / "weibo_book",
        ROOT / "backend" / "app" / "routers",
        ROOT / "backend" / "app" / "services",
    ]
    hits = []
    for d in scanned_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.py"):
            # 跳过入口/CLI/空文件
            if f.name in ("__init__.py", "cli.py", "__main__.py"):
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                for kw in redline_keywords:
                    if kw in line:
                        # 允许注释/文档字符串中提及（红线"反向引用"）
                        stripped = line.strip()
                        if stripped.startswith("#") or '"""' in line or "'''" in line:
                            continue
                        hits.append(f"{f.name}:{n}: [{kw}] {line[:60]}")
    return check(
        "23 业务代码无红线关键词",
        len(hits) == 0,
        f"{len(hits)} 处命中" if hits else "全清",
    )


# ====== v1.2.0 收口 D: 许可证与公开仓库策略锁 ======

def check_24_license_and_binary_policy() -> Check:
    """24. LICENSE 保持标准 MIT 条款。"""
    name = "24 标准 MIT 许可证"
    license_path = ROOT / "LICENSE"
    if not license_path.exists():
        return check(name, False, f"不存在: {license_path}")

    license_text = license_path.read_text(encoding="utf-8")
    if license_text.rstrip("\r\n") != STANDARD_MIT_LICENSE:
        return check(name, False, "LICENSE 不是固定的标准 MIT 正文")

    return check(name, True, "LICENSE 为标准 MIT 正文")


def check_25_readme_risk_notice_at_top() -> Check:
    """25. README.md 顶部有"风险须知"段 + 含 10 不做清单。"""
    p = ROOT / "README.md"
    if not p.exists():
        return check("25 README 风险须知", False, f"不存在: {p}")
    text = p.read_text(encoding="utf-8")
    # 顶部 2000 字符内必须含
    head = text[:2000]
    missing = []
    if "风险须知" not in head:
        missing.append("风险须知")
    if "评论发布" not in head or "多账号池" not in head:
        missing.append("10 不做清单")
    if missing:
        return check("25 README 风险须知", False, f"缺: {', '.join(missing)}")
    return check("25 README 风险须知", True, "顶部含 风险须知 + 10 不做")


def check_26_clinerules_has_core_rules() -> Check:
    """26. .clinerules 含 12 铁律 + 公开仓库策略。"""
    p = ROOT / ".clinerules"
    if not p.exists():
        return check("26 .clinerules 铁律", False, f"不存在: {p}")
    text = p.read_text(encoding="utf-8")
    missing = []
    if "12 条" not in text and "12 条铁律" not in text:
        missing.append("12 铁律")
    if "公开 GitHub 仓库" not in text:
        missing.append("公开仓库策略")
    if missing:
        return check("26 .clinerules 铁律", False, f"缺: {', '.join(missing)}")
    return check("26 .clinerules 铁律", True, "含 12 铁律 + 公开仓库策略")


def check_27_v120_stage4_route_b_locked() -> Check:
    """27. Windows WebView2 v147 旧路线技术决策已公开固化。"""
    p = ROOT / "docs" / "decisions" / "windows-webview2-v147.md"
    if not p.exists():
        return check(
            "27 WebView2 v147 技术决策",
            False,
            "不存在: docs/decisions/windows-webview2-v147.md",
        )
    text = p.read_text(encoding="utf-8")
    if text.rstrip("\r\n") != REVIEWED_WINDOWS_WEBVIEW2_V147_DECISION:
        return check(
            "27 WebView2 v147 技术决策",
            False,
            "docs/decisions/windows-webview2-v147.md 不是已审查的固定正文",
        )
    return check(
        "27 WebView2 v147 技术决策",
        True,
        f"公开技术决策已写入 {p.name}",
    )


# ====== B18 v1.2.0: 6 项新增封包链路硬约束（check_28 ~ check_33） ======

def check_28_requirements_pinned() -> Check:
    """28. requirements*.txt 用 == 锁版本（全 >= 算 fail）。

    防止依赖 major 版本升级破 frozen 兼容性（v1.1.4 抖音教训）。
    """
    files = [
        ROOT / "requirements.txt",
        ROOT / "requirements" / "common.txt",
        ROOT / "requirements" / "mac.txt",
        ROOT / "requirements" / "windows.txt",
    ]
    missing = [p.relative_to(ROOT).as_posix() for p in files if not p.exists()]
    if missing:
        return check("28 requirements pin ==", False, f"缺文件: {missing}")

    bad_lines = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("-r ") or s.startswith("--requirement "):
                continue
            if "==" in s:
                continue
            bad_lines.append(f"{rel}:L{n}: {s}")
    return check("28 requirements pin ==",
                 len(bad_lines) == 0,
                 "全 pin" if not bad_lines else f"{len(bad_lines)} 行未 pin: {bad_lines[:3]}")


def check_29_installer_version_consistent() -> Check:
    """29. installer.iss MyAppVersion 与 backend/app/version.py VERSION 一致。

    通过 #ifndef MyAppVersion 兜底（2.0.0），同时 build_exe.bat -DMyAppVersion 覆盖。
    """
    if sys.platform == "darwin":
        return check("29 macOS 无 Inno Setup 版本", True, "版本由 Info.plist 读取 version.py")
    p_iss = ROOT / "installer.iss"
    p_ver = ROOT / "backend" / "app" / "version.py"
    if not p_iss.exists() or not p_ver.exists():
        return check("29 installer.iss 版本一致", False, "缺文件")
    iss_text = p_iss.read_text(encoding="utf-8")
    ver_text = p_ver.read_text(encoding="utf-8")
    m_ver = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', ver_text)
    if not m_ver:
        return check("29 installer.iss 版本一致", False, "version.py 无 VERSION")
    target = m_ver.group(1)
    # installer.iss 现在用 #ifndef MyAppVersion + #define MyAppVersion "2.0.0" 兜底
    # 检查兜底值或传入值
    m_iss = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss_text)
    if not m_iss:
        return check("29 installer.iss 版本一致", False, "installer.iss 无 #define MyAppVersion")
    iss_ver = m_iss.group(1)
    if iss_ver != target:
        return check("29 installer.iss 版本一致", False, f"installer={iss_ver} vs version.py={target}")
    return check("29 installer.iss 版本一致", True, f"两边都是 {target}")


def check_30_build_spec_has_weibobook_templates() -> Check:
    """30. build.spec datas 含 weibo_book/templates。"""
    name = "build_mac.spec" if sys.platform == "darwin" else "build.spec"
    p = ROOT / name
    if not p.exists():
        return check(f"30 {name} datas 含 templates", False, f"缺 {name}")
    text = p.read_text(encoding="utf-8")
    has = '("weibo_book/templates"' in text or '"weibo_book/templates"' in text
    return check(f"30 {name} datas 含 templates", has,
                 "weibo_book/templates 在 datas" if has else "缺 weibo_book/templates")


def check_31_desktop_debug_off_when_frozen() -> Check:
    """31. desktop_app.py 的 frozen 分支必须关闭 DevTools。"""
    p = ROOT / "desktop_app.py"
    if not p.exists():
        return check("31 desktop debug=not frozen", False, "缺 desktop_app.py")
    import ast

    tree = ast.parse(p.read_text(encoding="utf-8"))
    helper_forces_off = False
    helper_is_used = False
    start_uses_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "should_enable_debug":
            for child in node.body:
                if not isinstance(child, ast.If) or not isinstance(child.test, ast.Name):
                    continue
                if child.test.id != "is_frozen":
                    continue
                helper_forces_off = any(
                    isinstance(item, ast.Return)
                    and isinstance(item.value, ast.Constant)
                    and item.value.value is False
                    for item in child.body
                )
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "debug_enabled"
            for target in node.targets
        ):
            call = node.value
            helper_is_used = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "should_enable_debug"
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "start":
                continue
            start_uses_result = start_uses_result or any(
                keyword.arg == "debug"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "debug_enabled"
                for keyword in node.keywords
            )
    has = helper_forces_off and helper_is_used and start_uses_result
    return check(
        "31 desktop frozen 关闭 debug",
        has,
        "frozen 强制 debug=False" if has else "未确认 frozen 强制关闭 debug",
    )


def check_32_jsapi_cookie_path_unified() -> Check:
    """32. Cookie 路径同源，并覆盖正式与开发文件的发布边界。"""
    p_js = ROOT / "js_api.py"
    p_paths = ROOT / "backend" / "app" / "platform_paths.py"
    p_login = ROOT / "weibo_book" / "login.py"
    p_gitignore = ROOT / ".gitignore"
    p_export = ROOT / "scripts" / "create_public_export.py"
    if not all(
        path.exists()
        for path in (p_js, p_paths, p_login, p_gitignore, p_export)
    ):
        return check("32 Cookie 路径与发布边界", False, "缺文件")
    js_text = p_js.read_text(encoding="utf-8")
    paths_text = p_paths.read_text(encoding="utf-8")
    login_text = p_login.read_text(encoding="utf-8")
    js_uses_platform_paths = "cookie_file_candidates" in js_text and "backend.app.platform_paths" in js_text
    paths_uses_login = "from weibo_book.login import get_cookie_file_path" in paths_text
    paths_exports_candidates = "def cookie_file_candidates" in paths_text
    login_default = "DEFAULT_COOKIE_FILE" in login_text and ".weibo_book_cookies" in login_text
    required_cookie_names = {
        ".weibo_book_cookies",
        ".weibo_book_cookies_dev",
    }
    ignored_names = {
        line.strip()
        for line in p_gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    export_names = _assigned_string_literals(
        p_export.read_text(encoding="utf-8"),
        "SENSITIVE_LOGIN_FILE_NAMES",
    )
    missing_ignores = sorted(required_cookie_names - ignored_names)
    missing_export_names = sorted(required_cookie_names - export_names)
    path_is_unified = (
        js_uses_platform_paths
        and paths_uses_login
        and paths_exports_candidates
        and login_default
    )
    if path_is_unified and not missing_ignores and not missing_export_names:
        return check(
            "32 Cookie 路径与发布边界",
            True,
            "路径同源，正式与开发 Cookie 均受 Git 和公开导出门禁保护",
        )
    return check(
        "32 Cookie 路径与发布边界",
        False,
        f"js_platform={js_uses_platform_paths} | paths_login={paths_uses_login} | "
        f"paths_candidates={paths_exports_candidates} | login_default={login_default} | "
        f"gitignore_missing={missing_ignores} | export_missing={missing_export_names}",
    )


def check_33_icon_ico_exists() -> Check:
    """33. icon.ico 存在 + size > 0。"""
    p = ROOT / "icon.ico"
    if not p.exists():
        return check("33 icon.ico 存在", False, f"不存在: {p}")
    if p.stat().st_size <= 0:
        return check("33 icon.ico 存在", False, f"size=0: {p}")
    return check("33 icon.ico 存在", True, f"{p.stat().st_size} bytes")


# ====== 主流程 ======
def main() -> int:
    # v1.1.4 用户决定：先做功能，最后再封包。
    # 配置层（spec/iss/bat/CHANGELOG）保留为硬约束（防丢失）
    # 烘焙层（PyInstaller + Inno Setup）暂不跑
    checks = [
        check_01_no_print_in_business(),
        check_02_chinese_error_keywords(),
        check_03_no_taskkill_in_chrome(),
        check_04_cookie_permissions(),
        check_05_partial_marker(),
        check_06_8_routers(),
        check_07_main_includes_all_routers(),
        check_08_ws_router_first(),
        check_09_rate_limit(),
        check_10_no_mode_toggle(),
        check_11_floating_log(),
        check_12_build_spec_exists(),
        check_13_installer_iss_exists(),
        check_14_build_bat_exists(),
        check_15_playwright_5_filenames(),
        check_16_changelog_v114(),
        check_17_root_agents_md(),
        check_18_no_relative_time(),
        check_19_tests_pass(),
        check_20_compileall(),
        # v1.2.0 V120-6: 红线扫描
        check_21_risks_md_exists(),
        check_22_clinerules_exists(),
        check_23_no_redline_keywords_in_business(),
        # v1.2.0 收口 D: 公开 repo 分发策略锁
        check_24_license_and_binary_policy(),
        check_25_readme_risk_notice_at_top(),
        check_26_clinerules_has_core_rules(),
        # v1.2.0 收口 E: V120-4 路线 A 不可行证据固化
        check_27_v120_stage4_route_b_locked(),
        # B18 v1.2.0: 封包链路 6 项新增
        check_28_requirements_pinned(),
        check_29_installer_version_consistent(),
        check_30_build_spec_has_weibobook_templates(),
        check_31_desktop_debug_off_when_frozen(),
        check_32_jsapi_cookie_path_unified(),
        check_33_icon_ico_exists(),
    ]
    print()
    print("=" * 60)
    print("  v2.0.0 release_check.py · 33 项硬约束")
    print("=" * 60)
    passed = 0
    for c in checks:
        sym = "✅" if c.ok else "❌"
        print(f"  {sym} {c.name:40s}  {c.msg}")
        if c.ok:
            passed += 1
    print()
    print(f"  {passed}/{len(checks)} PASSED")
    print()
    if passed == len(checks):
        print("  🎉 ALL GREEN — 可打包")
        return 0
    print(f"  ⚠️  {len(checks) - passed} 项 FAIL — 修复后重跑")
    return 1


if __name__ == "__main__":
    sys.exit(main())
