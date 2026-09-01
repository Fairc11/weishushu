"""结构化进度事件契约。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.app.services.task_manager import TaskManager, TaskRecord
from weibo_book.media import MediaDownloader
from weibo_book.models import MediaType, Post, PostMedia, UserInfo


class TaskProgressEventTests(unittest.TestCase):
    def test_event_is_saved_in_snapshot_and_broadcast(self):
        async def scenario():
            manager = TaskManager()
            manager._tasks["task"] = TaskRecord("task")
            queue = await manager.subscribe("task")
            await queue.get()
            event = {
                "phase": "extract", "pct": 0.25, "current": 23,
                "total": None, "unit": "post", "detail": "第 3 页",
                "elapsed_seconds": 4.2,
            }
            await manager.update_progress_event("task", event)
            message = await queue.get()
            return manager.snapshot("task"), message

        snapshot, message = asyncio.run(scenario())
        self.assertEqual(snapshot["progress_event"]["current"], 23)
        self.assertEqual(message["type"], "progress")
        self.assertEqual(message["event"]["phase"], "extract")
        self.assertIsNone(message["event"]["total"])


class WeiboBookStructuredProgressTests(unittest.TestCase):
    def test_generate_emits_all_six_phases_with_elapsed_time(self):
        from weibo_book.api import WeiboBook

        user = UserInfo(uid="1", screen_name="测试", avatar_url="")
        posts = [Post(bid="b1", uid="1", user_name="测试", user_avatar="", text="x")]
        events = []
        with tempfile.TemporaryDirectory() as td, \
             patch.object(WeiboBook, "extract", return_value={
                 "user": user, "posts": posts, "_partial": False,
                 "_pages_failed": 0, "_pages_total": 1, "_partial_reason": "",
             }), \
             patch.object(WeiboBook, "download_media", return_value={
                 "total": 0, "success": 0, "fail": 0, "failed": [],
             }), \
             patch("weibo_book.api.BookGenerator") as Generator, \
             patch("weibo_book.api.write_run_report", return_value=str(Path(td) / "report.md")):
            Generator.return_value.generate_markdown.return_value = str(Path(td) / "book.md")
            book = WeiboBook()
            book._progress_event_callback = events.append
            book.generate(
                "https://weibo.com/u/1", output_dir=td,
                formats=["md"], download_media=True,
            )

        phases = [event["phase"] for event in events]
        for phase in ("identify", "extract", "media", "generate", "report", "complete"):
            self.assertIn(phase, phases)
        self.assertTrue(all(event["elapsed_seconds"] >= 0 for event in events))
        generate_event = next(event for event in events if event["phase"] == "generate")
        self.assertEqual(generate_event["current"], 1)
        self.assertEqual(generate_event["total"], 1)


class MediaStructuredProgressTests(unittest.TestCase):
    def test_media_downloader_reports_real_completed_count(self):
        post = Post(
            bid="b1", uid="1", user_name="测试", user_avatar="", text="x",
            media=[
                PostMedia(type=MediaType.IMAGE, url="https://img.example/a.jpg"),
                PostMedia(type=MediaType.VIDEO, url="https://video.example/b.mp4"),
            ],
        )
        events = []
        with tempfile.TemporaryDirectory() as td, \
             patch("weibo_book.media.download_file", return_value=True):
            downloader = MediaDownloader(td, max_workers=1)
            downloader._progress_event_callback = events.append
            result = downloader.download_all([post])

        self.assertEqual(result["total"], 2)
        self.assertEqual(events[-1]["current"], 2)
        self.assertEqual(events[-1]["total"], 2)
        self.assertEqual(events[-1]["unit"], "media")


if __name__ == "__main__":
    unittest.main()
