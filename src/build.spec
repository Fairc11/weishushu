# -*- mode: python ; coding: utf-8 -*-
"""v2.0.0 PyInstaller onedir 配置（抖音 v1.4.2 范式）。

为什么 onedir（不 onefile）：
- 抖音 v1.4.1 踩坑：onefile 启动慢 + 路径解析难
- onedir 启动快 + 路径固定（sys.executable.parent = _internal/）
- 卸载/重装友好（直接删目录即可）
- Inno Setup 容易打包 onedir 目录

运行时路径（frozen）：
- sys.executable           = C:\Program Files\Weishushu\Weishushu.exe
- sys.executable.parent     = C:\Program Files\Weishushu\
- sys.executable.parent / "_internal"  = 程序内部资源
- 用户数据：%LOCALAPPDATA%\\Weishushu\\  （不写 Program Files，无写权限）
"""
import os
import sys
import glob
from pathlib import Path

# ====== 1. 资源文件 ======
datas = [
    # FastAPI 模板 + 静态资源
    ("backend/app/templates", "backend/app/templates"),
    ("backend/app/static", "backend/app/static"),
    # B05 v1.2.0: 微博书渲染模板（仿微博 APP HTML）—— config.weibobook_templates_dir 依赖
    ("weibo_book/templates", "weibo_book/templates"),
    # v2.0.0 封包离线自检 fixture（脱敏账号/媒体/最小档案/本地页）
    ("desktop/self_test/fixtures", "desktop/self_test/fixtures"),
]

# ====== 2. v1.1.3 D1：内置 Playwright Chromium ======
# 在 build_exe.bat 里执行 `playwright install chromium` 后，
# 自动找 %LOCALAPPDATA%\\ms-playwright\\chromium_headless_shell-XXX\\ 目录
def get_ms_playwright_chromium_dir():
    """return 第一个 chromium_headless_shell-* 目录路径或 None"""
    if sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    else:
        base = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(base):
        return None
    # 按目录名排序取最新
    candidates = glob.glob(os.path.join(base, "chromium_headless_shell-*"))
    if not candidates:
        # 退而求其次：chromium-* 完整包
        candidates = glob.glob(os.path.join(base, "chromium-*"))
    if not candidates:
        return None
    return sorted(candidates)[-1]


ms_pw_dir = get_ms_playwright_chromium_dir()
if ms_pw_dir:
    leaf = os.path.basename(ms_pw_dir)
    datas.append((ms_pw_dir, f"ms-playwright/{leaf}"))
    print(f"[D1] Bundling Playwright: {ms_pw_dir} -> _internal/ms-playwright/{leaf}/")
else:
    print("[D1] WARN: No Playwright Chromium found.")
    print("      Run `playwright install chromium` before build.")

# ====== 3. 排除 ======
# excludes 只接受 Python 模块名（包名/模块名），不接受文件名 glob。
# 防御敏感文件不靠 excludes（PyInstaller excludes 不认文件名 glob），
# 而是靠 datas 不包含敏感文件 + 用户数据写 %LOCALAPPDATA%，不打包进 EXE。
# B11 v1.2.0: 删掉所有 .env / *.log / cookies.* / test_*.py 等文件名 glob。
excludes = [
    # 真实 Python 模块名（可选的轻量清理）
    "pytest",
    "_pytest",
    "tests",            # tests 目录不进 EXE（仅当作为模块名匹配时生效）
    "docs",             # docs 同上
]

# 抖音 v1.4.1 教训：cryptography 不要排除（pywebview 依赖）
# pywebview[winforms] 依赖 cryptography / pywin32

# ====== 4. 隐藏导入 ======
hiddenimports = [
    # uvicorn
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    # crawl4weibo 子模块（PyInstaller 经常漏）
    "crawl4weibo",
    "crawl4weibo.models",
    "crawl4weibo.models.post",
    "crawl4weibo.models.comment",
    "cryptography",  # 不要排除（抖音 v1.4.1 教训）
    # B12 v1.2.0: 业务核心保险（PyInstaller 静态扫描漏 import 时的兜底）
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


# ====== 5. Analysis ======
a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(Path.cwd().resolve() / "packaging/pyinstaller/runtime_hook.py")],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)


# ====== 6. EXE + COLLECT（onedir 模式）======
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,    # onedir 必备：binaries 分离到 COLLECT
    name="Weishushu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # 抖音 v1.4.1 教训：frozen 后 sys.stdout=None，console=True 才能调试
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico" if os.path.exists("icon.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Weishushu",
)
