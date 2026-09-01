"""真实桌面壳探针。

没有桌面会话时返回专用环境不可用状态，不伪造通过。
有桌面会话时真正启动 pywebview，加载回环 HTTP fixture，
验证 JsApi、第二个登录窗口、测试 Cookie 写读清，以及正常关闭路径。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from backend.app.runtime_context import RuntimeContext
from desktop.self_test.functional import _local_http_server
from desktop.self_test.network_guard import assert_loopback
from desktop.self_test.schema import (
    ERROR_KIND_ENVIRONMENT_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    add_step,
    new_result,
    set_error,
    step,
    write_result,
)

ERROR_KIND_SHELL = "shell"

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"
SHELL_STEPS = (
    "shell_main_window",
    "shell_bridge",
    "shell_login_window",
    "shell_cookie",
    "shell_exit",
)
CURRENT_VERSION = "2.0.0"
SHELL_TIMEOUT = 15.0


def _desktop_session_unavailable() -> bool:
    """仅当明确知道没有桌面会话时返回 True。"""
    if os.environ.get("WEISHUSHU_SHELL_UNAVAILABLE") == "1":
        return True
    if sys.platform == "darwin" and os.environ.get("SSH_CONNECTION") and not os.environ.get("WEISHUSHU_ALLOW_SHELL_SMOKE"):
        return True
    return False


def _load_webview() -> Any:
    from desktop_app import load_webview

    module = load_webview()
    if module is None:
        raise RuntimeError("pywebview 不可用")
    return module


def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(message)


def _connect_loaded(window: Any, flag: threading.Event) -> None:
    """使用真实 pywebview event 契约：window.events.loaded += callback。"""
    window.events.loaded += lambda: flag.set()


def _close_windows(main_window: Any, login_window: Any) -> list[str]:
    errors: list[str] = []
    for window in (main_window, login_window):
        if window is None:
            continue
        try:
            window.destroy()
        except Exception as exc:
            errors.append(str(exc))
    return errors


def run_shell_smoke(
    context: RuntimeContext,
    output_path: Path,
) -> dict[str, Any]:
    result = new_result(
        build_commit=context.source_commit,
        profile=context.profile,
        platform=context.platform,
    )

    if _desktop_session_unavailable():
        add_step(
            result,
            step(
                "shell_main_window",
                STATUS_SKIPPED,
                message="当前环境没有可验证桌面会话",
                skip_reason="environment_unavailable",
            ),
        )
        set_error(result, ERROR_KIND_ENVIRONMENT_UNAVAILABLE, "没有可用桌面会话")
        write_result(output_path, result)
        return result

    webview = None
    main_window = None
    login_window = None
    try:
        webview = _load_webview()
        from js_api import JsApi

        js_api = JsApi()
        main_loaded = threading.Event()
        login_loaded = threading.Event()

        # 所有窗口都使用回环 HTTP fixture，不使用 file://。
        with _local_http_server(FIXTURES_ROOT) as base_url:
            assert_loopback(base_url)
            main_url = f"{base_url}/index.html"
            login_url = f"{base_url}/index.html"
            assert_loopback(main_url)
            assert_loopback(login_url)

            main_window = webview.create_window(
                title="微书薯自检",
                url=main_url,
                width=960,
                height=640,
                resizable=False,
                confirm_close=False,
                js_api=js_api,
            )
            add_step(result, step("shell_main_window", STATUS_PASSED, message="主窗口已创建"))
            js_api.set_window(main_window)
            _connect_loaded(main_window, main_loaded)

            login_window = webview.create_window(
                title="微书薯自检登录",
                url=login_url,
                width=800,
                height=600,
                resizable=False,
                confirm_close=False,
                js_api=js_api,
            )
            _connect_loaded(login_window, login_loaded)

            probe_result: list[tuple[bool, str]] = []

            def _probe() -> None:
                try:
                    _wait_until(main_loaded.is_set, SHELL_TIMEOUT, "主窗口加载超时")
                    _wait_until(login_loaded.is_set, SHELL_TIMEOUT, "登录窗口加载超时")

                    # 等待 pywebviewready 写入 DOM 的契约值。
                    def _bridge_value() -> str:
                        value = main_window.evaluate_js(
                            "document.body.getAttribute('data-weishushu-version') || ''"
                        )
                        return str(value)

                    _wait_until(
                        lambda: _bridge_value() == CURRENT_VERSION,
                        SHELL_TIMEOUT,
                        "pywebviewready/bridge 超时",
                    )
                    add_step(result, step("shell_bridge", STATUS_PASSED, message=f"JS bridge 返回 {CURRENT_VERSION}"))
                    add_step(result, step("shell_login_window", STATUS_PASSED, message="测试登录窗口已加载"))

                    login_window.evaluate_js(
                        "document.cookie='weishushu_self_test=ok; path=/'"
                    )
                    _wait_until(
                        lambda: "weishushu_self_test=ok" in str(login_window.evaluate_js("document.cookie")),
                        SHELL_TIMEOUT,
                        "Cookie 写入/读取超时",
                    )
                    login_window.evaluate_js(
                        "document.cookie='weishushu_self_test=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'"
                    )
                    _wait_until(
                        lambda: "weishushu_self_test" not in str(login_window.evaluate_js("document.cookie")),
                        SHELL_TIMEOUT,
                        "Cookie 清理超时",
                    )
                    add_step(result, step("shell_cookie", STATUS_PASSED, message="测试 Cookie 写读清通过"))

                    close_errors = _close_windows(main_window, login_window)
                    if close_errors:
                        raise RuntimeError("窗口关闭失败: " + "; ".join(close_errors))
                    add_step(result, step("shell_exit", STATUS_PASSED, message="两个窗口正常关闭"))
                    probe_result.append((True, ""))
                except Exception as exc:
                    close_errors = _close_windows(main_window, login_window)
                    message = str(exc) or type(exc).__name__
                    if close_errors:
                        message += "; " + "窗口关闭失败: " + "; ".join(close_errors)
                    probe_result.append((False, message))

            webview.start(_probe, debug=False)

            if not probe_result:
                raise RuntimeError("shell 探针回调未返回结果")
            ok, message = probe_result[-1]
            if not ok:
                set_error(result, ERROR_KIND_SHELL, f"桌面壳探针失败: {message}")
    except Exception as exc:
        if result["error_kind"] is None:
            message = str(exc) or type(exc).__name__
            set_error(result, ERROR_KIND_SHELL, f"桌面壳探针失败: {message}")
    finally:
        if main_window is not None or login_window is not None:
            _close_windows(main_window, login_window)

    if result["error_kind"] is None:
        step_names = [item["name"] for item in result["steps"]]
        if step_names == list(SHELL_STEPS) and all(
            item["status"] == STATUS_PASSED for item in result["steps"]
        ):
            result["message"] = "桌面壳探针通过"
        else:
            set_error(result, ERROR_KIND_SHELL, "桌面壳探针缺少完整通过步骤")
    write_result(output_path, result)
    return result
