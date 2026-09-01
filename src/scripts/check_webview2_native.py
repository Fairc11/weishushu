"""Windows WebView2 原生能力前置检查：comtypes + Win32 + WebView2。

技术决策与当前不实施的理由见 docs/decisions/windows-webview2-v147.md。
前 5 步：环境 + 运行时 + 基础能力
第 6 步（创建 WebView2 实体）放到路线 A 实施 step 2。

验收：
    python scripts/check_webview2_native.py
    期望输出 "All checks passed"
"""
from __future__ import annotations

import io
import sys

# Windows / GBK terminal fix：cmd 默认 GBK 编码下 print(✅/❌) 会直接 UnicodeEncodeError 崩
# 跟 scripts/release_check.py 同样的兜底
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


def check_platform() -> tuple[bool, str]:
    """1. 必须是 Windows。"""
    if sys.platform != "win32":
        return False, f"非 Windows 平台: {sys.platform}"
    return True, f"Windows 平台 OK (Python {sys.version.split()[0]})"


def check_comtypes() -> tuple[bool, str]:
    """2. comtypes 必须装（路线 A 调 WebView2 COM 必备）。"""
    try:
        import comtypes
        from comtypes.client import CreateObject
        ver = getattr(comtypes, "__version__", "?")
        return True, f"comtypes {ver} 已装"
    except ImportError:
        return False, "缺 comtypes（pip install comtypes）"


def check_pywin32() -> tuple[bool, str]:
    """3. pywin32 装好（路线 A 调 Win32 API 必备）。"""
    try:
        import win32api
        import win32con
        import win32gui
        return True, f"pywin32 {win32api.__version__ if hasattr(win32api, '__version__') else '已装'}"
    except ImportError:
        return False, "缺 pywin32（pip install pywin32）"


def check_webview2_runtime() -> tuple[bool, str]:
    """4. WebView2 Runtime 装好（pywebview 已用它，所以本机肯定有）。"""
    if sys.platform != "win32":
        return False, "非 Windows"
    try:
        import winreg
        # Edge WebView2 Runtime GUID: {F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}
        WV2_GUID = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WV2_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WV2_GUID}"),
        ]
        for hive, k in keys:
            try:
                with winreg.OpenKey(hive, k) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    return True, f"WebView2 Runtime {version}"
            except FileNotFoundError:
                continue
        return False, "WebView2 Runtime 未装（装 WebView2EvergreenBootstrapper.exe）"
    except ImportError:
        return False, "缺 winreg"


def check_create_offscreen_window() -> tuple[bool, str]:
    """5. 能否创建 Win32 离屏窗（路线 A 父窗基础）。"""
    if sys.platform != "win32":
        return False, "非 Windows"
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        # 注册一个临时窗口类
        WNDCLASS = type("WNDCLASS", (), {})  # 简化：直接用 CreateWindowExW
        hInst = ctypes.windll.kernel32.GetModuleHandleW(None)

        # "Static" 是 Windows 内置窗口类，不用注册
        hwnd = user32.CreateWindowExW(
            0, "Static", "FeasibilityTest", 0, 0, 0, 100, 100,
            None, None, hInst, None,
        )
        if not hwnd:
            err = ctypes.get_last_error()
            return False, f"CreateWindowExW 失败: GetLastError={err}"

        # 立刻销毁
        user32.DestroyWindow(hwnd)
        return True, f"Win32 离屏窗创建+销毁成功 (hwnd={hwnd})"
    except Exception as e:
        return False, f"Win32 调用异常: {type(e).__name__}: {e}"


def check_webview2_loader_dll() -> tuple[bool, str]:
    """6. WebView2Loader.dll 能加载（路线 A 调 WebView2 必备 DLL）。

    ⚠️ v147+ 微软已重命名/移除 WebView2Loader.dll → EBWebView/x64/EmbeddedBrowserWebView.dll
    本检查可能在新版 Runtime 上 FAIL——这是真实的可行性问题。
    """
    if sys.platform != "win32":
        return False, "非 Windows"
    try:
        import ctypes
        import os

        # 找旧名 WebView2Loader.dll
        candidates = [
            r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
            r"C:\Program Files\Microsoft\EdgeWebView\Application",
        ]
        found = []
        for d in candidates:
            if os.path.exists(d):
                try:
                    for sub in os.listdir(d):
                        full = os.path.join(d, sub, "WebView2Loader.dll")
                        if os.path.exists(full):
                            found.append(full)
                except OSError:
                    continue

        if not found:
            # 微软 v147+ 重构：WebView2Loader.dll 已移除，改用 EBWebView/x64/EmbeddedBrowserWebView.dll
            return False, "WebView2Loader.dll 不存在（v147+ 已移除/改名为 EBWebView/x64/EmbeddedBrowserWebView.dll）"

        loaded = 0
        for path in found:
            try:
                ctypes.CDLL(path)
                loaded += 1
            except OSError as e:
                return False, f"WebView2Loader.dll 加载失败 ({path}): {e}"

        return True, f"WebView2Loader.dll 加载成功 ({loaded} 处)"
    except Exception as e:
        return False, f"WebView2Loader 检查异常: {type(e).__name__}: {e}"


def check_webview2_create_env() -> tuple[bool, str]:
    """7. 能调 CreateCoreWebView2EnvironmentWithOptions（路线 A 真创建 WebView2）。

    ⚠️ v147+ 这个 C 导出函数已不存在于 EmbeddedBrowserWebView.dll + msedge.dll。
    新版 WebView2 必须走 .NET（WinForms/WPF）+ pythonnet 或纯 COM IDL import。
    """
    if sys.platform != "win32":
        return False, "非 Windows"
    try:
        import ctypes
        from ctypes import c_wchar_p, c_void_p

        # 试两个新 DLL
        candidates = [
            r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\147.0.3912.86\EBWebView\x64\EmbeddedBrowserWebView.dll",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\148.0.3967.54\msedge.dll",
        ]
        import os
        for dll_path in candidates:
            if not os.path.exists(dll_path):
                continue
            try:
                dll = ctypes.CDLL(dll_path)
            except OSError:
                continue
            try:
                fn = dll.CreateCoreWebView2EnvironmentWithOptions
                fn.restype = ctypes.c_long
                fn.argtypes = [c_wchar_p, c_wchar_p, c_void_p, c_void_p]
                hr = fn(None, None, None, None)
                if hr == 0:
                    return True, f"CreateCoreWebView2EnvironmentWithOptions 调通 ({dll_path})"
                return False, f"调通但 HRESULT={hr:#x}"
            except AttributeError:
                continue

        return False, "CreateCoreWebView2EnvironmentWithOptions 在 v147+ DLL 中已不存在——路线 A 不可行，回退路线 B"
    except Exception as e:
        return False, f"WebView2 env 创建异常: {type(e).__name__}: {e}"


def main() -> int:
    print("=" * 60)
    print("  V120-4 路线 A 前置验证 · 5 步基础检查")
    print("=" * 60)

    checks = [
        ("Step 1 平台是 Windows", check_platform),
        ("Step 2 comtypes 装好", check_comtypes),
        ("Step 3 pywin32 装好", check_pywin32),
        ("Step 4 WebView2 Runtime 装好", check_webview2_runtime),
        ("Step 5 Win32 离屏窗可创建", check_create_offscreen_window),
        ("Step 6 WebView2Loader.dll 可加载", check_webview2_loader_dll),
        ("Step 7 CreateCoreWebView2EnvironmentWithOptions 可调", check_webview2_create_env),
    ]

    passed = 0
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"未捕获异常: {type(e).__name__}: {e}"
        sym = "✅" if ok else "❌"
        print(f"  {sym} {name}: {msg}")
        if ok:
            passed += 1

    print()
    if passed == len(checks):
        print(f"  🎉 {passed}/{len(checks)} PASSED — 路线 A 前置可行")
        print()
        print("  下一步：实施 §6.2 顺序")
        print("    Day 1: 装 comtypes（已装 ✅） + 写 desktop_app_native.py 骨架")
        print("    Day 2: 跑通 1 个 WebView2 → 2 个")
        print("    Day 3: splitter 拖动 + bounds 同步")
        return 0
    else:
        print(f"  ⚠️  {len(checks) - passed} 项 FAIL")
        print()
        if not checks[1][1]()[0]:  # comtypes
            print("  装 comtypes: pip install comtypes")
        if not checks[2][1]()[0]:  # pywin32
            print("  装 pywin32: pip install pywin32")
        if not checks[3][1]()[0]:  # WebView2 Runtime
            print("  装 WebView2 Runtime: https://developer.microsoft.com/microsoft-edge/webview2/")
        return 1


if __name__ == "__main__":
    sys.exit(main())
