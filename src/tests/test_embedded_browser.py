"""Mac 内嵌微博浏览器的纯 Python 契约。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from desktop.browser.policy import is_allowed_browser_url
from desktop.browser.state import (
    BrowserLayoutState,
    clamp_split_position,
    fit_browser_frame,
    parse_browser_frame,
)


class BrowserNavigationPolicyTests(unittest.TestCase):
    def test_allows_weibo_and_login_hosts(self):
        for url in (
            "https://m.weibo.cn/",
            "https://weibo.com/u/123",
            "https://passport.weibo.com/visitor/visitor",
            "https://login.sina.com.cn/sso/login.php",
            "https://passport.weibo.cn/signin/login",
            "about:blank",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_allowed_browser_url(url))

    def test_rejects_lookalike_hosts_and_unsafe_schemes(self):
        for url in (
            "https://weibo.com.evil.example/",
            "https://evilweibo.com/",
            "https://example.com/",
            "file:///tmp/secret",
            "javascript:alert(1)",
            "data:text/html,test",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_allowed_browser_url(url))


class BrowserLayoutStateTests(unittest.TestCase):
    def test_content_slot_accepts_exact_finite_rectangle(self):
        frame = parse_browser_frame({
            "x": 812,
            "y": 118.5,
            "width": 452,
            "height": 670,
            "visible": True,
        })

        self.assertEqual(frame.as_dict(), {
            "x": 812.0,
            "y": 118.5,
            "width": 452.0,
            "height": 670.0,
            "visible": True,
        })

    def test_content_slot_rejects_missing_extra_and_invalid_values(self):
        invalid_frames = (
            {"x": 1, "y": 2, "width": 3, "height": 4},
            {"x": 1, "y": 2, "width": 3, "height": 4, "visible": True, "top": 2},
            {"x": -1, "y": 2, "width": 3, "height": 4, "visible": True},
            {"x": 1, "y": 2, "width": 0, "height": 4, "visible": True},
            {"x": 1, "y": 2, "width": float("inf"), "height": 4, "visible": True},
            {"x": 1, "y": 2, "width": 3, "height": 4, "visible": 1},
        )

        for value in invalid_frames:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_browser_frame(value)

    def test_content_slot_is_clamped_and_converted_to_cocoa_coordinates(self):
        frame = parse_browser_frame({
            "x": 800,
            "y": 100,
            "width": 500,
            "height": 700,
            "visible": True,
        })

        fitted = fit_browser_frame(frame, container_width=1280, container_height=800)

        self.assertEqual(fitted, (800.0, 0.0, 480.0, 700.0))

    def test_content_slot_outside_container_is_hidden(self):
        frame = parse_browser_frame({
            "x": 10,
            "y": 900,
            "width": 300,
            "height": 200,
            "visible": True,
        })

        self.assertIsNone(fit_browser_frame(frame, 1280, 800))

    def test_default_ratio_fits_minimum_window(self):
        left = clamp_split_position(960, ratio=0.62)

        self.assertGreaterEqual(left, 560)
        self.assertGreaterEqual(960 - 1 - left, 360)

    def test_split_position_clamps_both_sides(self):
        self.assertEqual(clamp_split_position(1280, ratio=0.1), 560)
        self.assertEqual(clamp_split_position(1280, ratio=0.95), 919)

    def test_user_ratio_survives_temporary_window_clamp(self):
        state = BrowserLayoutState(ratio=0.72)

        compact_left = state.split_position(960)
        expanded_left = state.split_position(1600)

        self.assertEqual(state.ratio, 0.72)
        self.assertEqual(compact_left, 599)
        self.assertEqual(expanded_left, 1151)

    def test_drag_updates_ratio_with_valid_bounds(self):
        state = BrowserLayoutState()

        state.remember_split_position(700, total_width=1280)
        self.assertAlmostEqual(state.ratio, 700 / 1279)

        state.remember_split_position(100, total_width=1280)
        self.assertAlmostEqual(state.ratio, 560 / 1279)

    def test_compact_window_keeps_both_panes_proportional(self):
        state = BrowserLayoutState(ratio=0.62)

        left = state.split_position(800)

        self.assertEqual(left, 495)
        self.assertEqual(800 - 1 - left, 304)


class EmbeddedBrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        from js_api import JsApi

        self.api = JsApi()
        self.controller = MagicMock()
        self.controller.show.return_value = {"ok": True, "url": "https://m.weibo.cn/"}
        self.controller.current_url.return_value = "https://m.weibo.cn/profile/123"
        self.controller.back.return_value = True
        self.controller.forward.return_value = True
        self.controller.reload.return_value = True
        self.controller.hide.return_value = True
        self.controller.set_frame.return_value = True
        self.api.set_browser_controller(self.controller)

    def test_open_uses_embedded_controller(self):
        result = self.api.open_browser_window()

        self.controller.show.assert_not_called()
        self.assertEqual(result, {
            "ok": False,
            "error": "该功能正在开发中。",
        })

    def test_navigation_and_close_delegate_to_controller(self):
        self.assertEqual(self.api.browser_back(), {"ok": True})
        self.assertEqual(self.api.browser_forward(), {"ok": True})
        self.assertEqual(self.api.refresh_browser(), {"ok": True})
        self.assertEqual(self.api.close_browser_window(), {"ok": True})

        self.controller.back.assert_called_once_with()
        self.controller.forward.assert_called_once_with()
        self.controller.reload.assert_called_once_with()
        self.controller.hide.assert_called_once_with()

    def test_copy_current_url_reads_embedded_controller(self):
        result = self.api.copy_url_to_main()

        self.assertEqual(result, {"ok": True, "url": "https://m.weibo.cn/profile/123"})
        self.assertEqual(self.api.get_copied_url(), "https://m.weibo.cn/profile/123")

    def test_content_slot_frame_delegates_to_embedded_controller(self):
        payload = {"x": 800, "y": 96, "width": 460, "height": 700, "visible": True}

        result = self.api.set_browser_frame(payload)

        self.assertEqual(result, {"ok": True})
        self.controller.set_frame.assert_called_once_with(payload)

    def test_content_slot_frame_fails_without_embedded_controller(self):
        from js_api import JsApi

        result = JsApi().set_browser_frame({
            "x": 800,
            "y": 96,
            "width": 460,
            "height": 700,
            "visible": True,
        })

        self.assertEqual(result, {"ok": False, "error": "当前平台没有内嵌浏览器"})


if __name__ == "__main__":
    unittest.main()
