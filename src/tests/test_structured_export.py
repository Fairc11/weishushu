"""v1.2.x 巡检新增：结构化导出 JSON / CSV。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weibo_book.generator import BookGenerator
from weibo_book.models import Post, UserInfo


def _user() -> UserInfo:
    return UserInfo(
        uid="123",
        screen_name="测试博主",
        avatar_url="https://wx1.sinaimg.cn/avatar.jpg",
    )


def _post(bid: str, text: str, created_at: datetime) -> Post:
    return Post(
        bid=bid,
        uid="123",
        user_name="测试博主",
        user_avatar="https://wx1.sinaimg.cn/avatar.jpg",
        text=text,
        created_at=created_at,
        reposts_count=1,
        comments_count=2,
        likes_count=3,
    )


class GenerateJsonTests(unittest.TestCase):
    def test_generate_json_writes_structured_file(self):
        with tempfile.TemporaryDirectory() as td:
            gen = BookGenerator(td)
            posts = [
                _post("A1", "第一条", datetime(2026, 1, 1, 10, 0, 0)),
                _post("A2", "第二条", datetime(2026, 1, 2, 10, 0, 0)),
            ]
            path = gen.generate_json(posts, _user())
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["user"]["screen_name"], "测试博主")
            self.assertEqual(len(data["posts"]), 2)
            self.assertEqual(data["posts"][0]["bid"], "A1")
            self.assertEqual(data["posts"][0]["text"], "第一条")
            self.assertEqual(data["posts"][1]["bid"], "A2")
            self.assertIn("generated_at", data)


class GenerateCsvTests(unittest.TestCase):
    def test_generate_csv_writes_one_row_per_post(self):
        with tempfile.TemporaryDirectory() as td:
            gen = BookGenerator(td)
            posts = [
                _post("B1", "第一, 条（含, 逗号）", datetime(2026, 1, 1, 10, 0, 0)),
                _post("B2", "第二\n行", datetime(2026, 1, 2, 10, 0, 0)),
            ]
            path = gen.generate_csv(posts, _user())
            with open(path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["bid"], "B1")
            self.assertEqual(rows[0]["text"], "第一, 条（含, 逗号）")
            self.assertEqual(rows[1]["bid"], "B2")
            self.assertEqual(rows[1]["text"], "第二\n行")
            self.assertEqual(rows[0]["reposts_count"], "1")
            self.assertEqual(rows[0]["comments_count"], "2")
            self.assertEqual(rows[0]["likes_count"], "3")


class ApiFormatsWiringTests(unittest.TestCase):
    def test_generate_dispatches_json_and_csv(self):
        """`api.generate` 在 formats 含 json / csv 时调用对应生成器。"""
        with tempfile.TemporaryDirectory() as td:
            from weibo_book import WeiboBook

            mock_extract_result = {
                "user": _user(),
                "posts": [_post("C1", "正文", datetime(2026, 1, 1, 10, 0, 0))],
                "_partial": False,
                "_pages_failed": 0,
                "_pages_total": 1,
                "_partial_reason": "",
            }
            with patch.object(WeiboBook, "extract", return_value=mock_extract_result), \
                 patch.object(WeiboBook, "download_media", return_value={"total": 0, "success": 0, "fail": 0, "failed": []}), \
                 patch.object(BookGenerator, "generate_json") as gj, \
                 patch.object(BookGenerator, "generate_csv") as gc:
                book = WeiboBook()
                result = book.generate(
                    url="https://weibo.com/u/123",
                    output_dir=td,
                    formats=["md", "json", "csv"],
                )
            gj.assert_called_once()
            gc.assert_called_once()
            self.assertIn("markdown", result)
            self.assertIn("json", result)
            self.assertIn("csv", result)


if __name__ == "__main__":
    unittest.main()
