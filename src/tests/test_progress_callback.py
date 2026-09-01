"""Progress callback 链路单测。覆盖 WeiboBook.generate 6 节点进度回调。

模拟：把 WeiboBook.generate 内调用的所有外部依赖（extract / download_media / generate_md/pdf/html / write_run_report）
全部 mock 掉，只验证 progress_callback 收到 0.01/0.50/0.55/0.75/0.80/1.0 六个节点。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProgressCallbackTests(unittest.TestCase):
    """验证 generate() 真的按 6 节点回调——这是 L1/A2 的核心交付物。"""

    def _run_generate(self, mock_extract, mock_download, mock_md, mock_pdf, mock_html, mock_report):
        """mock 掉所有 IO，跑一次 generate，捕获 progress_callback 序列。"""
        from weibo_book import WeiboBook
        from weibo_book.models import ExtractType, Post, UserInfo

        # 模拟 extract 返回 5 条帖子（Post 需要 user_name + user_avatar）
        user = UserInfo(uid="123", screen_name="test", avatar_url="", posts_count=5)
        posts = [Post(bid=f"b{i}", uid="123", user_name="test", user_avatar="", text=f"post {i}")
                 for i in range(5)]
        mock_extract.return_value = {"user": user, "posts": posts,
                                     "_partial": False, "_pages_failed": 0,
                                     "_pages_total": 1, "_partial_reason": ""}
        mock_download.return_value = {"total": 0, "success": 0, "fail": 0, "failed": []}
        mock_md.return_value = "/tmp/out.md"
        mock_pdf.return_value = "/tmp/out.pdf"
        mock_html.return_value = "/tmp/out.html"
        mock_report.return_value = "/tmp/report.md"

        captured: list[tuple[float, str]] = []

        with patch("weibo_book.api.WeiboBook.extract", mock_extract), \
             patch("weibo_book.api.WeiboBook.download_media", mock_download), \
             patch("weibo_book.api.BookGenerator") as mock_gen:
            mock_gen.return_value.generate_markdown = mock_md
            mock_gen.return_value.generate_pdf = mock_pdf
            mock_gen.return_value.generate_html = mock_html
            mock_report_path = mock_report

            book = WeiboBook()
            with patch("weibo_book.api.write_run_report", mock_report_path):
                book.generate(
                    url="https://weibo.com/u/123",
                    max_posts=5,
                    output_dir="/tmp/test_out",
                    formats=["md", "pdf", "html"],
                    download_media=True,
                    progress_callback=lambda pct, msg: captured.append((pct, msg)),
                )
        return captured

    def test_six_progress_nodes(self):
        """进度回调应至少收到 6 个节点：0.01 / 0.50 / 0.55 / 0.75 / 0.80 / 1.0"""
        captured = self._run_generate(
            MagicMock(), MagicMock(), MagicMock(return_value="/tmp/md"),
            MagicMock(return_value="/tmp/pdf"), MagicMock(return_value="/tmp/html"),
            MagicMock(return_value="/tmp/report"),
        )
        pcts = [c[0] for c in captured]
        # 必须包含 6 个关键节点（允许其他中间节点，但 6 个必到）
        for node in [0.01, 0.50, 0.55, 0.75, 0.80, 1.0]:
            self.assertIn(node, pcts, f"missing progress node {node}, got {pcts}")

    def test_final_node_is_100_percent(self):
        """最后一个节点必须是 1.0（生成完成）"""
        captured = self._run_generate(
            MagicMock(), MagicMock(), MagicMock(return_value="/tmp/md"),
            MagicMock(return_value="/tmp/pdf"), MagicMock(return_value="/tmp/html"),
            MagicMock(return_value="/tmp/report"),
        )
        self.assertEqual(captured[-1][0], 1.0)
        self.assertIn("✓", captured[-1][1])

    def test_no_posts_short_circuits_to_1(self):
        """0 条帖子的快路径：直接到 1.0 '无内容'"""
        from weibo_book import WeiboBook
        from weibo_book.models import UserInfo
        user = UserInfo(uid="123", screen_name="test", avatar_url="", posts_count=0)
        with patch("weibo_book.api.WeiboBook.extract") as mock_extract, \
             patch("weibo_book.api.write_run_report", return_value="/tmp/r"):
            mock_extract.return_value = {"user": user, "posts": [],
                                         "_partial": False, "_pages_failed": 0,
                                         "_pages_total": 0, "_partial_reason": ""}
            captured = []
            book = WeiboBook()
            book.generate(
                url="https://weibo.com/u/123",
                output_dir="/tmp/empty_out",
                progress_callback=lambda pct, msg: captured.append((pct, msg)),
            )
        self.assertEqual(captured[-1][0], 1.0)
        self.assertEqual(captured[-1][1], "无内容")


if __name__ == "__main__":
    unittest.main()
