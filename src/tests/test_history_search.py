"""v1.1.6 历史记录面板 + 全局搜索 单测。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class HistoryListTests(unittest.TestCase):
    """v1.1.6-1: 历史记录端点"""

    def setUp(self):
        from backend.app.routers.router_backup import _validate_output_dir
        self._validate = _validate_output_dir

    def test_list_glob_pattern_matches(self):
        """glob 模式：微博书_*.md"""
        import time
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            # 先建旧文件（mtime 早）
            (p / "微博书_测试_20260601_120000.md").write_text("# 微博书\n/status/456 /status/789", encoding="utf-8")
            old_mtime = (p / "微博书_测试_20260601_120000.md").stat().st_mtime
            time.sleep(0.2)  # Windows NTFS 至少 100ms
            (p / "微博书_测试_20260602_180000.md").write_text("# 微博书\n/status/123", encoding="utf-8")
            new_mtime = (p / "微博书_测试_20260602_180000.md").stat().st_mtime
            self.assertGreater(new_mtime, old_mtime, "mtime 顺序必须可分辨")
            (p / "其他文件.md").write_text("不要扫我", encoding="utf-8")
            # 跑 list 端点
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/list?path={td}")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertEqual(data["total"], 2)
                # 最新的在前（mtime 降序）
                self.assertIn("20260602", data["entries"][0]["filename"])
                self.assertIn("20260601", data["entries"][1]["filename"])
                # BID 计数（粗估）
                self.assertEqual(data["entries"][0]["bids_count"], 1)  # 1 个 /status/
                self.assertEqual(data["entries"][1]["bids_count"], 2)  # 2 个 /status/

    def test_list_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/list?path={td}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["total"], 0)

    def test_list_invalid_path_400(self):
        from fastapi.testclient import TestClient
        from backend.app.main import app
        with TestClient(app) as client:
            r = client.post("/api/backup/list?path=relative/path")
            self.assertEqual(r.status_code, 400)

    def test_list_missing_directory_returns_404(self):
        """目录父级存在但目标目录不存在 → 返回 404，而不是服务端异常。"""
        from fastapi.testclient import TestClient
        from backend.app.main import app
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing-history"
            with TestClient(app) as client:
                r = client.post(f"/api/backup/list?path={missing}")
                self.assertEqual(r.status_code, 404)
                self.assertIn("目录不存在", r.json()["detail"])


class SearchTests(unittest.TestCase):
    """v1.1.6-2: 全局搜索端点"""

    def test_search_basic_hit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "微博书_A_20260602.md").write_text(
                "第 1 行\n第二行有 麻花辫 关键词\n第 3 行无关\n", encoding="utf-8"
            )
            (p / "微博书_B_20260601.md").write_text(
                "另一文件\n没有这个 麻花辫 词\n", encoding="utf-8"
            )
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/search?path={td}&q=麻花辫")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertEqual(data["query"], "麻花辫")
                self.assertEqual(data["files_scanned"], 2)
                # 2 个命中（2 文件 × 1 行）
                self.assertEqual(len(data["hits"]), 2)
                # 命中行包含"麻花辫"
                for hit in data["hits"]:
                    self.assertIn("麻花辫", hit["line_text"])

    def test_search_no_hits(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "微博书_A.md").write_text("纯文本无关键词", encoding="utf-8")
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/search?path={td}&q=不存在的词")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.json()["hits"]), 0)

    def test_search_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "微博书_A.md").write_text("hello WORLD\n再见 world\n", encoding="utf-8")
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/search?path={td}&q=WORLD")
                self.assertEqual(len(r.json()["hits"]), 2)  # 大小写不敏感

    def test_search_context_includes_neighbors(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "微博书_A.md").write_text(
                "前一行\n命中行 test\n后一行\n", encoding="utf-8"
            )
            from fastapi.testclient import TestClient
            from backend.app.main import app
            with TestClient(app) as client:
                r = client.post(f"/api/backup/search?path={td}&q=test")
                hit = r.json()["hits"][0]
                self.assertEqual(hit["line_no"], 2)
                self.assertIn("前一行", hit["context_before"])
                self.assertIn("命中行 test", hit["line_text"])
                self.assertIn("后一行", hit["context_after"])

    def test_search_missing_directory_returns_404(self):
        """目录父级存在但目标目录不存在 → 返回 404，而不是服务端异常。"""
        from fastapi.testclient import TestClient
        from backend.app.main import app
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing-history"
            with TestClient(app) as client:
                r = client.post(f"/api/backup/search?path={missing}&q=test")
                self.assertEqual(r.status_code, 404)
                self.assertIn("目录不存在", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
