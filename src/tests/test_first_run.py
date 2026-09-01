"""v1.2.0 V120-1: 首启风险须知 API 测试。

覆盖：
- 首次弹：标记文件不存在 → /api/first-run/check 返 accepted: false
- 接受后跳过：标记文件存在 → accepted: true
- 拒绝报错：/api/first-run/accept 写标记文件 + 返 marker_path
- JSON 持久化：accept 后再 check 应返 true
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app


def _fake_marker_path(tmp: Path, name: str = "first_run_v2.0.1.json") -> Path:
    return tmp / name


class FirstRunCheckTests(unittest.TestCase):
    """V120-1: /api/first-run/check 行为。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_check_returns_false_when_marker_absent(self):
        """首次：标记文件不存在 → accepted: false → 前端应弹模态。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake = _fake_marker_path(Path(td))
            with patch(
                "backend.app.services.first_run._marker_path",
                return_value=fake,
            ):
                r = self.client.post("/api/first-run/check")
                self.assertEqual(r.status_code, 200, f"check 失败: {r.text}")
                self.assertFalse(
                    r.json()["accepted"],
                    f"首次应 accepted=false，实际: {r.json()}",
                )

    def test_check_returns_true_when_marker_exists(self):
        """接受后：标记文件存在 → accepted: true → 前端跳过模态。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake = _fake_marker_path(Path(td))
            fake.write_text('{"schema_version": 1}', encoding="utf-8")
            with patch(
                "backend.app.services.first_run._marker_path",
                return_value=fake,
            ):
                r = self.client.post("/api/first-run/check")
                self.assertEqual(r.status_code, 200)
                self.assertTrue(
                    r.json()["accepted"],
                    f"已接受应 accepted=true，实际: {r.json()}",
                )


class FirstRunAcceptTests(unittest.TestCase):
    """V120-1: /api/first-run/accept 写标记 + 返路径。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_accept_creates_marker_file(self):
        """接受：写标记文件 + 返 accepted: true + marker_path。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = _fake_marker_path(Path(td))
            with patch(
                "backend.app.services.first_run._marker_path",
                return_value=target,
            ):
                r = self.client.post("/api/first-run/accept")
                self.assertEqual(r.status_code, 200, f"accept 失败: {r.text}")
                body = r.json()
                self.assertTrue(body["accepted"])
                self.assertEqual(body["marker_path"], str(target))

                # 标记文件应存在
                self.assertTrue(target.exists(), f"标记文件未创建: {target}")

                # 标记文件内容应合法（含 schema_version=1, version=2.0.1, accepted_at）
                data = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(data["schema_version"], 1)
                self.assertEqual(data["version"], "2.0.1")
                self.assertIn("accepted_at", data)
                self.assertIsInstance(data["accepted_at"], (int, float))

    def test_marker_persistence_round_trip(self):
        """持久化：accept → check 应一致。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = _fake_marker_path(Path(td))
            with patch(
                "backend.app.services.first_run._marker_path",
                return_value=target,
            ):
                r1 = self.client.post("/api/first-run/accept")
                self.assertTrue(r1.json()["accepted"])
                r2 = self.client.post("/api/first-run/check")
                self.assertTrue(r2.json()["accepted"])


if __name__ == "__main__":
    unittest.main()
