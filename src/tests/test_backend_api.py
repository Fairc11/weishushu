"""FastAPI 端点冒烟测试。8 router 全覆盖。"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app


class BackendAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("version", body)

    def test_index_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        text = r.text
        # 登录收纳到顶栏，主流程保持预览、设置与进度三个区域。
        for step in ("login-menu-toggle", "login-menu", "step-2", "step-3", "step-progress"):
            self.assertIn(step, text)
        self.assertIn("ws_client.js", text)
        # 验证模式选择已移除
        self.assertNotIn("mode-toggle", text)
        self.assertNotIn("选择模式", text)

    def test_static_assets(self):
        for path in (
            "/static/css/tokens.css",
            "/static/css/base.css",
            "/static/css/shell.css",
            "/static/css/components.css",
            "/static/css/workflows.css",
            "/static/css/responsive.css",
            "/static/js/desktop_bridge.js",
            "/static/js/api_client.js",
            "/static/js/modules/state.js",
            "/static/js/modules/feedback.js",
            "/static/js/modules/login.js",
            "/static/js/modules/archive.js",
            "/static/js/modules/tasks.js",
            "/static/js/modules/desktop.js",
            "/static/js/ws_client.js",
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f"{path} not served")
            self.assertGreater(len(r.text), 100, f"{path} too small")

    def test_cdn_whitelist_blocks_evil(self):
        r = self.client.get("/api/assets/img", params={"url": "https://evil.com/x.jpg"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not allowed", r.json()["detail"])

    def test_cdn_whitelist_allows_sinaimg(self):
        # 白名单放行，但实际 URL 不存在 → 应走到 httpx 404
        r = self.client.get("/api/assets/img", params={"url": "https://wx1.sinaimg.cn/original/notexist.jpg"})
        self.assertIn(r.status_code, (404, 502), f"got {r.status_code}")

    def test_login_status_shape(self):
        r = self.client.get("/api/login/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("logged_in", body)

    def test_login_status_rejects_expired_cookie_file(self):
        with patch(
            "backend.app.routers.router_login.load_cookies",
            return_value={"cookies": [{"name": "SUB", "value": "stale"}]},
        ), patch(
            "weibo_book.login.validate_stored_cookies",
            return_value=False,
        ):
            r = self.client.get("/api/login/status")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json(),
            {"logged_in": False, "cookie_source": "expired"},
        )

    def test_login_status_accepts_valid_cookie_file(self):
        with patch(
            "backend.app.routers.router_login.load_cookies",
            return_value={"cookies": [{"name": "SUB", "value": "valid"}]},
        ), patch(
            "weibo_book.login.validate_stored_cookies",
            return_value=True,
        ):
            r = self.client.get("/api/login/status")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json(),
            {"logged_in": True, "cookie_source": "file"},
        )

    def test_profile_resolve_is_disabled_for_current_version(self):
        r = self.client.post("/api/profile/resolve", json={})
        self.assertEqual(r.status_code, 501)
        self.assertEqual(
            r.json()["detail"],
            "该功能正在开发中。",
        )

    def test_scrape_preview_requires_url(self):
        r = self.client.post("/api/scrape/preview", json={})
        self.assertEqual(r.status_code, 422)

    def test_scrape_start_requires_url(self):
        r = self.client.post("/api/scrape/start", json={})
        self.assertEqual(r.status_code, 422)

    def test_tasks_404(self):
        r = self.client.get("/api/tasks/nonexistent_task_id")
        self.assertEqual(r.status_code, 404)

    def test_logs_tail(self):
        r = self.client.get("/api/logs/", params={"tail": 5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("entries", body)
        self.assertIsInstance(body["entries"], list)

    def test_all_routers_registered(self):
        """回归：plan 要求 8 router 都挂上，防止遗漏。"""
        paths = {r.path for r in app.routes}
        required = {
            "/api/profile/resolve",
            "/api/scrape/preview",
            "/api/scrape/start",
            "/api/login/qrcode",
            "/api/login/chrome",
            "/api/login/status",
            "/api/download/media",
            "/api/logs/",
            "/api/tasks/{task_id}",
            "/api/tasks/{task_id}/cancel",
            "/api/assets/img",
            "/ws/tasks/{task_id}",
        }
        missing = required - paths
        self.assertEqual(missing, set(), f"missing routes: {missing}")


if __name__ == "__main__":
    unittest.main()
