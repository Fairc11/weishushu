"""v1.2.0 M3-4: 后台任务"等完成"测试。

覆盖：
- /api/scrape/start 后台任务能完成（done）
- /api/backup/start 首次备份能完成（done）
- /api/backup/start 增量备份能完成（done）
- 失败时任务状态是 error，错误信息能回到前端

旧测试只验证 task_id 返回，不验证后台真跑完。本文件补这块。
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
from backend.app.services.persistent_task_store import PersistentTaskStore
from backend.app.services.task_manager import TaskManager, task_manager
from backend.app.schemas import ArchiveFolderInspection
from weibo_book.archive.sync import SyncResult
from weibo_book.errors import OperationCancelled

ROOT = Path(__file__).resolve().parents[1]


def _wait_for_task_done(
    task_id: str,
    timeout: float = 15.0,
    poll: float = 0.1,
    manager: TaskManager | None = None,
):
    """轮询直到 task 状态是 done/error/cancelled 或超时。

    返回最终 snapshot（可能 None 如果 task_id 找不到）。
    """
    active_manager = task_manager if manager is None else manager
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = active_manager.snapshot(task_id)
        if snap and snap["state"] in ("done", "error", "cancelled"):
            return snap
        time.sleep(poll)
    return active_manager.snapshot(task_id)


# ====== 1. /api/scrape/start 正常路径 ======

class ScrapeStartLifecycleTests(unittest.TestCase):
    """当前版本保留 scrape 路由，但不得创建任务。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_scrape_start_runs_to_done(self):
        """普通主页提取返回 501，不调用业务核心。"""
        with patch("weibo_book.WeiboBook") as MockBook:
            before = set(task_manager._tasks)
            r = self.client.post("/api/scrape/start", json={
                "url": "https://weibo.com/u/123",
                "max_posts": 2,
                "formats": ["md"],
            })
            self.assertEqual(r.status_code, 501)
            self.assertEqual(set(task_manager._tasks), before)
            MockBook.assert_not_called()

    def test_scrape_start_failure_returns_error_state(self):
        """禁用路由不会进入原有失败路径。"""
        with patch("weibo_book.WeiboBook") as MockBook:
            r = self.client.post("/api/scrape/start", json={
                "url": "https://weibo.com/u/123",
            })
            self.assertEqual(r.status_code, 501)
            MockBook.assert_not_called()

    def test_scrape_start_passes_image_quality_to_weibobook(self):
        """禁用路由不解析图片质量到业务对象。"""
        with patch("weibo_book.WeiboBook") as MockBook:
            r = self.client.post("/api/scrape/start", json={
                "url": "https://weibo.com/u/123",
                "formats": ["md"],
                "image_quality": "original",
            })
            self.assertEqual(r.status_code, 501)
            MockBook.assert_not_called()


class ScrapeProgressLoopContractTests(unittest.TestCase):
    """抓取任务进度回调必须能从同步生成线程安全地回到主事件循环。"""

    def test_scraper_progress_uses_captured_running_loop(self):
        src = (ROOT / "backend" / "app" / "routers" / "router_scraper.py").read_text(encoding="utf-8")
        self.assertIn("loop = asyncio.get_running_loop()", src)
        self.assertIn("asyncio.run_coroutine_threadsafe", src)

        progress_start = src.index("def _progress_cb")
        progress_end = src.index("async def _run", progress_start)
        progress_body = src[progress_start:progress_end]
        self.assertNotIn("asyncio.get_event_loop()", progress_body)

    def test_scraper_generate_runs_off_event_loop(self):
        src = (ROOT / "backend" / "app" / "routers" / "router_scraper.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.to_thread(", src)
        self.assertIn("book.generate,", src)


class PersonalArchiveLifecycleTests(unittest.TestCase):
    """本人归档后台任务的 done/error/cancelled 终态。"""

    def setUp(self):
        """为每项用例隔离路由所用的持久任务管理器。"""
        self._task_state_dir = tempfile.TemporaryDirectory()
        self.archive_dir = (Path(self._task_state_dir.name) / "archive").resolve()
        self.manager = TaskManager(PersistentTaskStore(
            Path(self._task_state_dir.name) / "active-personal-archive-task.json"
        ))
        self._personal_service = PersonalArchiveTaskService(manager=self.manager)
        self._patches = (
            patch("backend.app.routers.router_backup.task_manager", self.manager),
            patch("backend.app.routers.router_tasks.task_manager", self.manager),
            patch(
                "backend.app.routers.router_tasks.personal_archive_tasks",
                self._personal_service,
            ),
        )
        for active_patch in self._patches:
            active_patch.start()
        self.assertIsNone(self.manager._persistent_store.load())
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for active_patch in reversed(self._patches):
            active_patch.stop()
        self._task_state_dir.cleanup()

    @staticmethod
    def _route_patches(path: str, completed: threading.Event | None = None):

        def inspect_for_run(_path, *, current_uid):
            return ArchiveFolderInspection(
                state="archive" if completed is not None and completed.is_set() else "empty",
                path=path,
                uid=current_uid if completed is not None and completed.is_set() else "",
            )

        return (
            patch("backend.app.routers.router_backup.whoami", return_value={
                "uid": "10001", "screen_name": "本人",
            }),
            patch(
                "backend.app.routers.router_backup.inspect_archive_folder",
                side_effect=inspect_for_run,
            ),
            patch(
                "backend.app.routers.router_backup.build_personal_archive_dependencies",
                return_value=(MagicMock(), None),
            ),
        )

    def test_personal_archive_runs_off_event_loop_and_done(self):
        sync = MagicMock()
        completed = threading.Event()

        def complete_sync(_mode):
            completed.set()
            return SyncResult("create", 1, 0, 0, 0, [])

        sync.run.side_effect = complete_sync
        output_dir = str(self.archive_dir)
        patches = self._route_patches(output_dir, completed)
        with patches[0], patches[1], patches[2], patch(
            "backend.app.routers.router_backup.PersonalArchiveSync",
            return_value=sync,
        ), patch(
            "backend.app.routers.router_backup.render_personal_archive",
            return_value=[],
        ), patch(
            "backend.app.routers.router_backup._ensure_mode_allowed"
        ), patch(
            "backend.app.services.personal_archive_tasks.PersonalArchiveTaskService._ensure_mode_allowed"
        ):
            response = self.client.post("/api/backup/start", json={
                "output_dir": output_dir, "mode": "create",
                "pacing_mode": "standard", "keep_awake_when_plugged": False,
            })
            self.assertEqual(response.status_code, 200, response.text)
            snap = _wait_for_task_done(response.json()["task_id"], manager=self.manager)
        self.assertEqual(snap["state"], "done")
        self.assertEqual(snap["result"]["new_posts"], 1)

    def test_personal_archive_failure_is_error(self):
        sync = MagicMock()
        sync.run.side_effect = RuntimeError("同步失败")
        output_dir = str(self.archive_dir)
        patches = self._route_patches(output_dir)
        with patches[0], patches[1], patches[2], patch(
            "backend.app.routers.router_backup.PersonalArchiveSync",
            return_value=sync,
        ), patch(
            "backend.app.routers.router_backup._ensure_mode_allowed"
        ), patch(
            "backend.app.services.personal_archive_tasks.PersonalArchiveTaskService._ensure_mode_allowed"
        ):
            response = self.client.post("/api/backup/start", json={
                "output_dir": output_dir, "mode": "create",
                "pacing_mode": "standard", "keep_awake_when_plugged": False,
            })
            self.assertEqual(response.status_code, 200, response.text)
            snap = _wait_for_task_done(response.json()["task_id"], manager=self.manager)
        self.assertEqual(snap["state"], "error")
        self.assertEqual(snap["error"], "任务执行失败，请查看日志后重试")

    def test_personal_archive_cancel_sets_sync_token_and_cancelled(self):
        class BlockingSync:
            def __init__(self, *args, cancel_requested, **kwargs):
                self.cancel_requested = cancel_requested

            def run(self, mode):
                deadline = time.time() + 5
                while time.time() < deadline:
                    if self.cancel_requested():
                        raise OperationCancelled("任务已取消")
                    time.sleep(0.01)
                raise AssertionError("取消信号未传给同步器")

        output_dir = str(self.archive_dir)
        patches = self._route_patches(output_dir)
        with patches[0], patches[1], patches[2], patch(
            "backend.app.routers.router_backup.PersonalArchiveSync", BlockingSync,
        ), patch(
            "backend.app.routers.router_backup._ensure_mode_allowed"
        ), patch(
            "backend.app.services.personal_archive_tasks.PersonalArchiveTaskService._ensure_mode_allowed"
        ):
            response = self.client.post("/api/backup/start", json={
                "output_dir": output_dir, "mode": "create",
                "pacing_mode": "standard", "keep_awake_when_plugged": False,
            })
            self.assertEqual(response.status_code, 200, response.text)
            task_id = response.json()["task_id"]
            cancelled = self.client.post(f"/api/tasks/{task_id}/cancel")
            snap = _wait_for_task_done(task_id, manager=self.manager)
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["cancelled"])
        self.assertEqual(snap["state"], "cancelled")


class PersonalArchiveProgressContractTests(unittest.TestCase):
    def test_archive_sync_runs_in_worker_and_all_phases_are_defined(self):
        service_src = (ROOT / "backend" / "app" / "services" / "personal_archive_tasks.py").read_text(encoding="utf-8")
        sync_src = (ROOT / "weibo_book" / "archive" / "sync.py").read_text(encoding="utf-8")
        self.assertIn("await asyncio.to_thread(sync.run, mode)", service_src)
        for phase in (
            "identify", "discover", "extract", "comments", "media",
            "generate", "complete",
        ):
            self.assertIn(f'"{phase}"', sync_src)
        self.assertIn("total=None", sync_src)


class SafeTaskErrorTests(unittest.TestCase):
    def test_unknown_background_error_does_not_leak_secrets(self):
        import asyncio
        from backend.app.services.task_manager import TaskManager, TaskRecord
        from unittest.mock import patch

        async def scenario():
            manager = TaskManager()
            manager._tasks["task"] = TaskRecord("task")

            async def fail():
                raise RuntimeError(
                    "https://evil.test/api?token=secret /Users/private/file SUB=secret"
                )

            with patch("backend.app.services.task_manager.task_manager", manager):
                from backend.app.services.task_manager import run_in_background
                await run_in_background("task", fail)
            return manager.snapshot("task")

        snapshot = asyncio.run(scenario())
        self.assertEqual(snapshot["error"], "任务执行失败，请查看日志后重试")
        self.assertNotIn("secret", snapshot["error"])
        self.assertNotIn("https://", snapshot["error"])
        self.assertNotIn("/Users/", snapshot["error"])

    def test_known_chinese_weibo_error_is_sanitized(self):
        from backend.app.services.task_manager import safe_task_error
        from weibo_book.errors import WeiboError, WeiboErrorKind

        message = safe_task_error(WeiboError(
            "同步失败 https://evil.test/a?token=urlsecret "
            "/Users/private/file C:\\private\\file "
            "access_token=tokensecret Authorization: Bearer bearersecret SUB=cookiesecret",
            kind=WeiboErrorKind.NETWORK,
        ))
        self.assertIn("同步失败", message)
        self.assertNotIn("secret", message.lower())
        self.assertNotIn("https://", message)
        self.assertNotIn("/Users/", message)
        self.assertNotIn("C:\\private", message)

    def test_all_observed_auth_cookie_values_are_hidden_case_sensitively(self):
        from backend.app.services.task_manager import safe_task_error
        from weibo_book.errors import WeiboError

        value = safe_task_error(WeiboError(
            "认证失败 SUB=a; SUBP=b; SSOLoginState=c; SCF=d; ALF=e; other=keep"
        ))
        for secret in ("=a", "=b", "=c", "=d", "=e"):
            self.assertNotIn(secret, value)
        for key in ("SUB", "SUBP", "SSOLoginState", "SCF", "ALF"):
            self.assertIn(f"{key}=[已隐藏]", value)
        self.assertIn("other=keep", value)


if __name__ == "__main__":
    unittest.main()
