"""v1.1.5 一键备份本人微博单测。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IndexFileTests(unittest.TestCase):
    """v1.1.5 索引文件读写 + 路径校验"""

    def setUp(self):
        from backend.app.routers.router_backup import read_index, write_index, _validate_output_dir
        from backend.app.schemas import BackupIndex
        self.read_index = read_index
        self.write_index = write_index
        self._validate_output_dir = _validate_output_dir
        self.BackupIndex = BackupIndex

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            idx = self.BackupIndex(
                uid="123",
                screen_name="测试",
                last_backup_at=1234567890.0,
                last_backup_count=10,
                total_backed_up=10,
                bids=["a", "b", "c"],
                versions={"a": "微博书_测试_20260602.md"},
            )
            self.write_index(p, idx)
            # 文件存在
            self.assertTrue((p / ".weishushu_index.json").exists())
            # 读回一致
            loaded = self.read_index(p)
            self.assertEqual(loaded.uid, "123")
            self.assertEqual(loaded.bids, ["a", "b", "c"])

    def test_read_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self.assertIsNone(self.read_index(p))

    def test_atomic_write_no_leftover_tmp(self):
        """原子写：成功后 .tmp 不残留"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            idx = self.BackupIndex(uid="1", bids=["x"])
            self.write_index(p, idx)
            tmp = p / ".weishushu_index.json.tmp"
            self.assertFalse(tmp.exists())

    def test_corrupted_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / ".weishushu_index.json").write_text("NOT JSON {{{", encoding="utf-8")
            self.assertIsNone(self.read_index(p))

    def test_validate_rejects_relative_path(self):
        with self.assertRaises(Exception) as ctx:
            self._validate_output_dir("not/absolute")
        self.assertIn("绝对路径", str(ctx.exception.detail))

    def test_validate_rejects_nonexistent_parent(self):
        with self.assertRaises(Exception) as ctx:
            self._validate_output_dir("C:/nope/nope/output")
        self.assertIn("父目录", str(ctx.exception.detail))


class WhoamiTests(unittest.TestCase):
    """v1.1.5 whoami：无 cookie 抛 AUTH"""

    def test_no_cookie_raises_auth(self):
        from backend.app.services.whoami import whoami
        from weibo_book.errors import WeiboError, WeiboErrorKind

        with patch("backend.app.services.whoami.WeiboBook") as MockBook:
            book = MagicMock()
            book.ensure_login.return_value = None  # 无 cookie
            MockBook.return_value = book
            with self.assertRaises(WeiboError) as ctx:
                whoami()
            self.assertEqual(ctx.exception.kind, WeiboErrorKind.AUTH)

    def test_cookie_exists_returns_user(self):
        from backend.app.services.whoami import whoami

        with patch("backend.app.services.whoami.WeiboBook") as MockBook, \
             patch("backend.app.services.whoami.WeiboExtractor") as MockExt:
            book = MagicMock()
            book.ensure_login.return_value = "SUB=abc"
            MockBook.return_value = book

            ext = MagicMock()
            ext.client.session.get.return_value.json.return_value = {
                "data": {"login": True, "uid": "1234567890"}
            }
            ext.get_user_info.return_value = MagicMock(
                uid="1234567890",
                screen_name="测试用户",
                avatar_url="https://wx1.sinaimg.cn/a.jpg",
                followers_count=1000,
                following_count=100,
                posts_count=200,
                verified=False,
                description="",
            )
            MockExt.return_value = ext

            result = whoami()
            self.assertEqual(result["uid"], "1234567890")
            self.assertEqual(result["screen_name"], "测试用户")


class IncrementalDiffTests(unittest.TestCase):
    """v1.1.5 增量检测：BID 集合差"""

    def test_diff_new_bids(self):
        existing = {"a", "b", "c"}
        all_bids = ["a", "b", "c", "d", "e"]
        new = [b for b in all_bids if b not in existing]
        self.assertEqual(new, ["d", "e"])

    def test_diff_all_new(self):
        existing = set()
        all_bids = ["a", "b"]
        new = [b for b in all_bids if b not in existing]
        self.assertEqual(new, ["a", "b"])

    def test_diff_no_new(self):
        existing = {"a", "b", "c"}
        all_bids = ["a", "b", "c"]
        new = [b for b in all_bids if b not in existing]
        self.assertEqual(new, [])

    def test_index_merge_dedup(self):
        """merge 时按 BID 去重，保留首次出现顺序。"""
        old = ["a", "b", "c"]
        new = ["b", "d", "c", "e"]
        merged = list(dict.fromkeys(old + new))
        self.assertEqual(merged, ["a", "b", "c", "d", "e"])


if __name__ == "__main__":
    unittest.main()
