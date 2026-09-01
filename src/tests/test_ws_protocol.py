"""WebSocket 协议测试。"""

import asyncio
import json
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.task_manager import task_manager


class WSProtocolTests(unittest.TestCase):
    """用 TestClient 测 WS：创建任务 → 连 WS → 期待 snapshot 消息 → 关掉。"""

    def setUp(self):
        self.client = TestClient(app)
        self.task_id = uuid.uuid4().hex[:12]
        # 直接造一个 pending 任务（避免真跑 weibo_book）
        from backend.app.services.task_manager import TaskRecord
        rec = TaskRecord(self.task_id)
        task_manager._tasks[self.task_id] = rec

    def tearDown(self):
        task_manager._tasks.pop(self.task_id, None)

    def test_ws_sends_snapshot_on_connect(self):
        """连上 WS 立即收一帧 snapshot。"""
        with self.client.websocket_connect(f"/ws/tasks/{self.task_id}") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "snapshot")
            self.assertEqual(msg["id"], self.task_id)
            self.assertEqual(msg["state"], "pending")

    def test_ws_404_for_unknown_task(self):
        with self.client.websocket_connect("/ws/tasks/nonexistent_zzz") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            self.assertIn("not found", msg["error"])

    def test_ws_progress_message(self):
        """广播 progress 消息 → WS 客户端能收到。"""
        async def runner():
            await task_manager.update_progress(self.task_id, 0.5, "测试中")

        with self.client.websocket_connect(f"/ws/tasks/{self.task_id}") as ws:
            snapshot = ws.receive_json()  # 吃掉第一帧
            self.assertEqual(snapshot["type"], "snapshot")

            # 触发 progress
            asyncio.run(runner())

            # 循环收，跳过 log 心跳，直到收到 progress / done / error
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "progress":
                    self.assertAlmostEqual(msg["pct"], 0.5)
                    self.assertEqual(msg["msg"], "测试中")
                    return
                # 跳过 log / snapshot
            self.fail("没收到 progress 消息")


if __name__ == "__main__":
    unittest.main()
