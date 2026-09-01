from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


def load_reports_module():
    path = Path(__file__).resolve().parents[1] / "weibo_book" / "reports.py"
    spec = importlib.util.spec_from_file_location("reports_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReportTests(unittest.TestCase):
    def test_write_run_report_records_success_and_failure_context(self):
        reports = load_reports_module()

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            report_path = reports.write_run_report(
                output_dir=output_dir,
                started_at=datetime(2026, 6, 1, 12, 0, 0),
                finished_at=datetime(2026, 6, 1, 12, 0, 3),
                url="https://weibo.com/u/123",
                params={"max_posts": 3, "formats": ["md"]},
                result={
                    "posts_count": 2,
                    "markdown": "book.md",
                    "pdf": None,
                    "html": None,
                    "media_summary": {
                        "total": 3,
                        "success": 2,
                        "fail": 1,
                        "failed": [{"url": "https://example.com/a.jpg", "dest": "media/a.jpg"}],
                    },
                },
                error="示例错误",
            )

            content = Path(report_path).read_text(encoding="utf-8")

        self.assertIn("https://weibo.com/u/123", content)
        self.assertIn("示例错误", content)
        self.assertIn("媒体下载：2 成功 / 1 失败 / 3 总计", content)
        self.assertIn("book.md", content)


if __name__ == "__main__":
    unittest.main()
