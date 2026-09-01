"""pywebview 桌面壳冒烟（不真弹窗）。验证 desktop_app.js_api 模块结构 + 关键参数。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class JsApiSmokeTests(unittest.TestCase):
    """T3 v1.1.1：pywebview 桥接层冒烟（不实际启动窗口）"""

    def test_js_api_class_exists(self):
        from js_api import JsApi
        api = JsApi()
        self.assertIsNotNone(api)

    def test_get_version_returns_string(self):
        from js_api import JsApi
        v = JsApi().get_version()
        self.assertIsInstance(v, str)
        self.assertEqual(v, "2.0.1")

    def test_set_window_stores_reference(self):
        from js_api import JsApi
        api = JsApi()
        # Mock window 对象
        class FakeWindow:
            pass
        api.set_window(FakeWindow())
        self.assertIsNotNone(api._window)

    def test_required_methods_exist(self):
        """plan §3.3 要求：minimize / close_window / open_folder / get_version"""
        from js_api import JsApi
        api = JsApi()
        for method in ("minimize", "close_window", "open_folder", "get_version", "show_in_folder", "get_platform", "toggle_maximize"):
            self.assertTrue(callable(getattr(api, method, None)), f"missing method: {method}")


class DesktopAppSmokeTests(unittest.TestCase):
    """T3 v1.1.1：desktop_app 模块结构 + find_free_port"""

    def test_find_free_port(self):
        from desktop_app import find_free_port
        port = find_free_port(18099, 18199)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 18099)
        self.assertLessEqual(port, 18199)

    def test_find_free_port_no_available(self):
        from desktop_app import find_free_port
        # start=end=一个被占的端口，应抛
        # 用 socket 模拟占一个
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 18999))
        s.listen(1)
        try:
            with self.assertRaises(RuntimeError):
                find_free_port(18999, 18999)
        finally:
            s.close()

    def test_create_window_params_match(self):
        """plan §3.1 要求：frameless=False, confirm_close=False, easy_drag=True, text_select=True"""
        import inspect
        from desktop_app import main
        # main() 内调 create_window，验源码里有这些参数
        import desktop_app
        src = inspect.getsource(desktop_app)
        for kw in ("frameless=False", "confirm_close=False", "easy_drag=True", "text_select=True", "min_size=(960, 640)"):
            self.assertIn(kw, src, f"missing {kw} in desktop_app.main()")

    def test_select_gui_backend_only_for_windows_frozen(self):
        from desktop_app import select_gui_backend

        self.assertEqual(select_gui_backend(is_frozen=True, platform="win32"), "edgechromium")
        self.assertIsNone(select_gui_backend(is_frozen=False, platform="win32"))
        self.assertIsNone(select_gui_backend(is_frozen=True, platform="darwin"))
        self.assertIsNone(select_gui_backend(is_frozen=False, platform="darwin"))

    def test_debug_can_be_disabled_for_clean_visual_checks(self):
        from desktop_app import should_enable_debug

        self.assertTrue(should_enable_debug(False, {}))
        self.assertFalse(should_enable_debug(False, {"WEISHUSHU_DEBUG": "0"}))
        self.assertFalse(should_enable_debug(False, {"WEISHUSHU_DEBUG": "off"}))
        self.assertFalse(should_enable_debug(True, {"WEISHUSHU_DEBUG": "1"}))

    def test_visual_window_size_override_is_strict_and_clamped(self):
        from desktop_app import resolve_window_size

        self.assertEqual(resolve_window_size({}), (1280, 820))
        self.assertEqual(resolve_window_size({"WEISHUSHU_WINDOW_SIZE": "960x640"}), (960, 640))
        self.assertEqual(resolve_window_size({"WEISHUSHU_WINDOW_SIZE": "1600x1000"}), (1600, 1000))
        self.assertEqual(resolve_window_size({"WEISHUSHU_WINDOW_SIZE": "900x600"}), (1280, 820))
        self.assertEqual(resolve_window_size({"WEISHUSHU_WINDOW_SIZE": "wide"}), (1280, 820))

    def test_webview2_check_only_runs_on_windows(self):
        from desktop_app import should_check_webview2

        self.assertTrue(should_check_webview2("win32"))
        self.assertFalse(should_check_webview2("darwin"))
        self.assertFalse(should_check_webview2("linux"))

    def test_backend_disables_uvicorn_console_log_configuration(self):
        """冻结版没有可用控制台时，Uvicorn 不得重配 stdout/stderr。"""
        import desktop_app

        configured = {}

        class FakeConfig:
            def __init__(self, *args, **kwargs):
                configured.update(kwargs)
                if kwargs.get("log_config", "missing") is not None:
                    raise AssertionError("Uvicorn 必须禁用默认控制台日志配置")

        class FakeServer:
            def __init__(self, config):
                self.config = config

            def run(self):
                pass

        class FakeUvicorn:
            Config = FakeConfig
            Server = FakeServer

        port_holder = []
        ready_event = MagicMock()
        startup_errors = []
        with patch.dict(sys.modules, {"uvicorn": FakeUvicorn}), \
             patch.object(desktop_app, "find_free_port", return_value=18080):
            desktop_app.start_backend(port_holder, ready_event, startup_errors)

        self.assertEqual(startup_errors, [])
        self.assertEqual(port_holder, [18080])
        self.assertIsNone(configured["log_config"])

    def test_desktop_app_does_not_import_webview_at_module_top_level(self):
        import ast

        desktop_app_path = Path(__file__).resolve().parents[1] / "desktop_app.py"
        tree = ast.parse(desktop_app_path.read_text(encoding="utf-8"))
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]

        imported_modules = []
        for node in top_level_imports:
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif node.module:
                imported_modules.append(node.module)

        self.assertNotIn("webview", imported_modules)

    def test_load_webview_returns_none_when_import_fails(self):
        from desktop_app import load_webview

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "webview":
                raise ImportError("missing webview")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            self.assertIsNone(load_webview())

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS 运行 AppKit 同窗分支")
    def test_main_skips_webview2_check_on_macos(self):
        import desktop_app

        fake_webview = MagicMock()
        fake_webview.create_window.return_value = MagicMock()

        with patch.object(desktop_app, "start_backend", side_effect=lambda holder, event, errors: (holder.append(18080), event.set())), \
             patch.object(desktop_app, "load_webview", return_value=fake_webview), \
             patch.object(desktop_app, "should_check_webview2", return_value=False) as mock_should_check, \
             patch("sys.platform", "darwin"):
            result = desktop_app.main()

        self.assertEqual(result, 0)
        mock_should_check.assert_called_once_with("darwin")
        fake_webview.start.assert_called_once_with(debug=True, gui=None)

    def test_main_uses_edgechromium_for_windows_frozen(self):
        import desktop_app

        fake_webview = MagicMock()
        fake_webview.create_window.return_value = MagicMock()

        class FrozenSwitch:
            def __enter__(self):
                self.had = hasattr(sys, "frozen")
                self.orig = getattr(sys, "frozen", None)
                sys.frozen = True

            def __exit__(self, *args):
                if self.had:
                    sys.frozen = self.orig
                elif hasattr(sys, "frozen"):
                    del sys.frozen

        with FrozenSwitch(), \
             patch.object(desktop_app, "start_backend", side_effect=lambda holder, event, errors: (holder.append(18080), event.set())), \
             patch.object(desktop_app, "load_webview", return_value=fake_webview), \
             patch.object(desktop_app, "should_check_webview2", return_value=True), \
             patch("backend.app.services.setup_check.check_webview2_installed", return_value=True), \
             patch("sys.platform", "win32"):
            result = desktop_app.main()

        self.assertEqual(result, 0)
        fake_webview.start.assert_called_once_with(debug=False, gui="edgechromium")

    def test_main_does_not_open_window_after_backend_startup_failure(self):
        import desktop_app

        fake_webview = MagicMock()

        def fail_backend(holder, event, errors):
            errors.append("模拟端口绑定失败")
            event.set()

        with patch.object(desktop_app, "start_backend", side_effect=fail_backend), \
             patch.object(desktop_app, "load_webview", return_value=fake_webview):
            result = desktop_app.main()

        self.assertEqual(result, 1)
        fake_webview.create_window.assert_not_called()


class RunPyEntryTests(unittest.TestCase):
    """T6 v1.1.1：run.py dev/frozen 双路径入口。

    B07 v1.2.0: run.py 不再自带 _setup_logging 和 ptu_boot.log，
    统一交给 desktop_app._setup_logging()（避免 handler 重复 + 路径两套）。
    这里只验极简入口：import + 调 desktop_main()。
    """

    def test_run_has_runtime_dir(self):
        """B07 v1.2.0: run.py 是极简入口（_is_frozen + desktop_app + 透传 main），不再带日志初始化。"""
        import inspect
        import run
        src = inspect.getsource(run)
        # B07 v1.2.0: run.py 不再自己拼 Weishushu 路径（统一去 backend.app.config.log_dir）
        tokens = ("_is_frozen", "desktop_app", "desktop_main")
        for token in tokens:
            self.assertIn(token, src, f"run.py missing {token}")
        # B07 v1.2.0: run.py 不再带 ptu_boot.log / _setup_logging（统一去 desktop_app）
        self.assertNotIn(
            "ptu_boot.log", src,
            "B07: run.py 不应再写 ptu_boot.log（统一去 desktop_app）",
        )
        self.assertNotIn(
            "_setup_logging", src,
            "B07: run.py 不应再定义/调 _setup_logging（统一去 desktop_app）",
        )

    def test_run_dispatches_multiprocessing_child_before_desktop_main(self):
        import multiprocessing
        import runpy
        import types

        events = []
        fake_desktop = types.SimpleNamespace(
            main=lambda: events.append("desktop_main") or 0
        )
        run_path = Path(__file__).resolve().parents[1] / "run.py"
        with patch.object(
            multiprocessing,
            "freeze_support",
            side_effect=lambda: events.append("freeze_support"),
        ), patch.dict(os.environ, {}, clear=True), \
             patch.dict(sys.modules, {"desktop_app": fake_desktop}), \
             self.assertRaises(SystemExit) as exit_info:
            runpy.run_path(str(run_path), run_name="__main__")

        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(events, ["freeze_support", "desktop_main"])


if __name__ == "__main__":
    unittest.main()
