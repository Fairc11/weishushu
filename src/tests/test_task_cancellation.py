"""后台任务取消的协作式停止契约。"""

import asyncio
import contextlib
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TaskCancellationTests(unittest.TestCase):
    def test_personal_archive_pause_is_cooperative_and_persisted(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental",
                output_dir=str((path / "微博书").resolve()),
            )
            record = manager.get(task_id)
            worker = asyncio.create_task(asyncio.sleep(60))
            record._asyncio_task = worker

            self.assertTrue(await manager.request_pause(task_id))
            self.assertEqual(manager.snapshot(task_id)["state"], "pausing")
            self.assertTrue(record._pause_event.is_set())
            self.assertFalse(record._cancel_event.is_set())
            self.assertFalse(worker.cancelled())
            self.assertEqual(store.load().state, "pausing")

            await manager.set_waiting_resume(
                task_id, pause_reason="user_requested"
            )
            self.assertEqual(manager.snapshot(task_id)["state"], "waiting_resume")
            self.assertEqual(store.load().state, "waiting_resume")
            self.assertEqual(store.load().pause_reason, "user_requested")
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_personal_archive_cancel_waits_for_worker_safe_point(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="create",
                output_dir=str((path / "微博书").resolve()),
            )
            record = manager.get(task_id)
            worker = asyncio.create_task(asyncio.sleep(60))
            record._asyncio_task = worker

            self.assertTrue(await manager.request_cancel(task_id))
            self.assertEqual(manager.snapshot(task_id)["state"], "cancelling")
            self.assertTrue(record._cancel_event.is_set())
            self.assertFalse(worker.cancelled())
            self.assertEqual(store.load().state, "cancelling")

            await manager.set_cancelled(task_id)
            self.assertEqual(manager.snapshot(task_id)["state"], "cancelled")
            self.assertIsNone(store.load())
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            for handle in manager._gc_timers.values():
                handle.cancel()

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_only_one_unfinished_personal_archive_task_is_allowed(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager
        from weibo_book.errors import WeiboError

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            await manager.create_personal_archive(
                mode="create", output_dir=str((path / "一").resolve())
            )
            with self.assertRaisesRegex(WeiboError, "已有未完成的本人归档任务"):
                await manager.create_personal_archive(
                    mode="rebuild", output_dir=str((path / "二").resolve())
                )

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_terminal_personal_archive_releases_single_task_slot(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            first = await manager.create_personal_archive(
                mode="create", output_dir=str((path / "一").resolve())
            )
            await manager.set_done(first, {"ok": True})
            self.assertIsNone(store.load())
            second = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "二").resolve())
            )
            self.assertNotEqual(first, second)
            for handle in manager._gc_timers.values():
                handle.cancel()

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_progress_does_not_overwrite_personal_archive_transition_state(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )
            await manager.request_pause(task_id)
            await manager.update_progress(task_id, 0.5, "迟到的进度")
            await manager.update_progress_event(task_id, {
                "phase": "extract", "pct": 0.6, "current": 6,
                "total": 10, "unit": "post", "detail": "迟到的结构化进度",
            })
            self.assertEqual(manager.snapshot(task_id)["state"], "pausing")
            self.assertEqual(store.load().state, "pausing")

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_structured_progress_updates_persistent_safe_summary(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )
            await manager.update_progress_event(task_id, {
                "phase": "extract", "pct": 0.3, "current": 3,
                "total": 10, "unit": "post", "detail": "已提交 3 条微博",
            })
            saved = store.load()
            self.assertEqual(saved.progress_current, 3)
            self.assertEqual(saved.progress_total, 10)
            self.assertEqual(saved.progress_unit, "post")
            self.assertEqual(saved.saved_content, "已提交 3 条微博")

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_background_wrapper_turns_pause_signal_into_waiting_resume(self):
        from unittest.mock import patch

        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import OperationPaused

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )

            async def pause_at_safe_point():
                raise OperationPaused("任务已暂停")

            with patch("backend.app.services.task_manager.task_manager", manager):
                await run_in_background(task_id, pause_at_safe_point)
            self.assertEqual(manager.snapshot(task_id)["state"], "waiting_resume")
            self.assertEqual(store.load().state, "waiting_resume")
            self.assertEqual(store.load().pause_reason, "user_requested")

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_background_wrapper_preserves_explicit_pause_reason(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import OperationPaused

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )

            async def pause_at_safe_point():
                raise OperationPaused(
                    "登录状态已失效，请重新登录后继续",
                    pause_reason="authentication_required",
                )

            await run_in_background(task_id, pause_at_safe_point, manager=manager)

            self.assertEqual(manager.snapshot(task_id)["state"], "waiting_resume")
            self.assertEqual(store.load().pause_reason, "authentication_required")

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_persistent_auth_and_rate_limit_errors_pause_without_retry(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import WeiboError, WeiboErrorKind

        async def scenario(path: Path, kind, reason):
            store = PersistentTaskStore(path / f"{kind.value}.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / kind.value).resolve())
            )
            calls = 0

            async def fail_once():
                nonlocal calls
                calls += 1
                raise WeiboError(
                    "登录状态已失效" if kind is WeiboErrorKind.AUTH else "平台限制了当前请求频率",
                    kind=kind,
                )

            await run_in_background(task_id, fail_once, manager=manager)

            self.assertEqual(calls, 1)
            self.assertEqual(manager.snapshot(task_id)["state"], "waiting_resume")
            self.assertEqual(store.load().state, "waiting_resume")
            self.assertEqual(store.load().pause_reason, reason)

        with TemporaryDirectory() as td:
            path = Path(td)
            asyncio.run(scenario(path, WeiboErrorKind.AUTH, "authentication_required"))
            asyncio.run(scenario(path, WeiboErrorKind.RATE_LIMIT, "rate_limited"))

    def test_nonpersistent_auth_error_keeps_existing_error_semantics(self):
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import WeiboError, WeiboErrorKind

        async def scenario():
            manager = TaskManager()
            task_id = await manager.create()

            async def fail():
                raise WeiboError("登录状态已失效", kind=WeiboErrorKind.AUTH)

            await run_in_background(task_id, fail, manager=manager)

            self.assertEqual(manager.snapshot(task_id)["state"], "error")
            for handle in manager._gc_timers.values():
                handle.cancel()

        asyncio.run(scenario())

    def test_only_recoverable_persistent_error_can_resume(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import WeiboError, WeiboErrorKind

        async def scenario(path: Path, recoverable: bool):
            store = PersistentTaskStore(path / f"{recoverable}.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental",
                output_dir=str((path / str(recoverable)).resolve()),
            )

            async def fail():
                raise WeiboError(
                    "归档数据校验失败",
                    kind=WeiboErrorKind.PARSE,
                    recoverable=recoverable,
                )

            await run_in_background(task_id, fail, manager=manager)

            saved = store.load()
            self.assertEqual(saved.state, "error")
            self.assertIs(saved.error_recoverable, recoverable)
            self.assertIs(
                await manager.prepare_persistent_resume(task_id),
                recoverable,
            )

        with TemporaryDirectory() as td:
            path = Path(td)
            asyncio.run(scenario(path, False))
            asyncio.run(scenario(path, True))

    def test_nonrecoverable_persistent_error_still_allows_safe_abandon_transition(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )
            await manager.set_error(
                task_id,
                "归档数据校验失败",
                error_recoverable=False,
            )

            await manager.set_abandoned(task_id)

            self.assertEqual(manager.snapshot(task_id)["state"], "abandoned")
            self.assertIsNone(store.load())
            for handle in manager._gc_timers.values():
                handle.cancel()

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_background_wrapper_turns_cooperative_stop_into_cancelled(self):
        from unittest.mock import patch

        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager, run_in_background
        from weibo_book.errors import OperationCancelled

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="create", output_dir=str((path / "微博书").resolve())
            )
            await manager.request_cancel(task_id)

            async def stop_at_safe_point():
                raise OperationCancelled("任务已取消")

            with patch("backend.app.services.task_manager.task_manager", manager):
                await run_in_background(task_id, stop_at_safe_point)
            self.assertEqual(manager.snapshot(task_id)["state"], "cancelled")
            self.assertIsNone(store.load())
            for handle in manager._gc_timers.values():
                handle.cancel()

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_waiting_record_can_be_restored_without_starting_worker(self):
        from dataclasses import replace

        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            creating = TaskManager(persistent_store=store)
            task_id = await creating.create_personal_archive(
                mode="rebuild", output_dir=str((path / "微博书").resolve())
            )
            saved = replace(
                store.load(),
                state="waiting_resume",
                pause_reason="unexpected_exit",
            )
            store.save(saved)

            restored = TaskManager(persistent_store=store)
            self.assertTrue(await restored.restore_waiting_record(saved))
            snapshot = restored.snapshot(task_id)
            self.assertEqual(snapshot["state"], "waiting_resume")
            self.assertEqual(snapshot["mode"], "rebuild")
            self.assertIsNone(restored.get(task_id)._asyncio_task)
            self.assertFalse(await restored.restore_waiting_record(saved))

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_late_pause_signal_cannot_overwrite_cancelling(self):
        from backend.app.services.persistent_task_store import PersistentTaskStore
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="incremental", output_dir=str((path / "微博书").resolve())
            )
            self.assertTrue(await manager.request_pause(task_id))
            self.assertTrue(await manager.request_cancel(task_id))

            await manager.set_waiting_resume(
                task_id, pause_reason="迟到的暂停信号"
            )

            self.assertEqual(manager.snapshot(task_id)["state"], "cancelling")
            self.assertEqual(store.load().state, "cancelling")

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_done_is_not_broadcast_when_persistent_cleanup_fails(self):
        from backend.app.services.persistent_task_store import (
            PersistentTaskStore,
            PersistentTaskStoreError,
        )
        from backend.app.services.task_manager import TaskManager

        async def scenario(path: Path):
            store = PersistentTaskStore(path / "task.json")
            manager = TaskManager(persistent_store=store)
            task_id = await manager.create_personal_archive(
                mode="create", output_dir=str((path / "微博书").resolve())
            )
            queue = await manager.subscribe(task_id)
            await queue.get()

            def fail_clear():
                raise PersistentTaskStoreError("模拟清理失败")

            store.clear = fail_clear
            with self.assertRaisesRegex(PersistentTaskStoreError, "模拟清理失败"):
                await manager.set_done(task_id, {"ok": True})

            self.assertEqual(manager.snapshot(task_id)["state"], "running")
            self.assertTrue(queue.empty())

        with TemporaryDirectory() as td:
            asyncio.run(scenario(Path(td)))

    def test_cancel_sets_worker_stop_signal(self):
        from backend.app.services.task_manager import TaskManager

        async def scenario():
            manager = TaskManager()
            task_id = await manager.create()
            record = manager.get(task_id)
            self.assertIsNotNone(record)
            self.assertFalse(record._cancel_event.is_set())

            self.assertTrue(await manager.cancel(task_id))
            self.assertTrue(record._cancel_event.is_set())

            for handle in manager._gc_timers.values():
                handle.cancel()

        asyncio.run(scenario())

    def test_cancel_is_rejected_after_fixed_outputs_enter_commit(self):
        from backend.app.services.task_manager import TaskManager

        async def scenario():
            manager = TaskManager()
            task_id = await manager.create()
            record = manager.get(task_id)
            self.assertIsNotNone(record)
            self.assertTrue(record.try_begin_commit())

            self.assertFalse(await manager.cancel(task_id))
            self.assertFalse(record._cancel_event.is_set())
            self.assertEqual(manager.snapshot(task_id)["state"], "pending")

        asyncio.run(scenario())

    def test_generate_stops_before_starting_io_when_cancelled(self):
        from weibo_book import WeiboBook
        from weibo_book.errors import OperationCancelled

        book = WeiboBook()
        book._cancel_event = threading.Event()
        book._cancel_event.set()
        book.extract = MagicMock()

        with self.assertRaises(OperationCancelled):
            book.generate("https://weibo.com/u/123", output_dir="/tmp/weishushu-cancelled")

        book.extract.assert_not_called()

    def test_router_hands_stop_signal_to_worker_book(self):
        root = Path(__file__).resolve().parents[1]
        scraper = (
            root / "backend" / "app" / "routers" / "router_scraper.py"
        ).read_text(encoding="utf-8")
        service = (
            root / "backend" / "app" / "services" / "personal_archive_tasks.py"
        ).read_text(encoding="utf-8")
        self.assertIn("book._cancel_event = rec._cancel_event", scraper)
        self.assertIn("cancel_requested=record._cancel_event.is_set", service)
        self.assertIn("pause_requested=record._pause_event.is_set", service)

    def test_frontend_exposes_task_cancel(self):
        from frontend_assets import frontend_bundle_asset

        root = Path(__file__).resolve().parents[1]
        api_source = (root / "backend" / "app" / "static" / "js" / "api_client.js").read_text(encoding="utf-8")
        app_source = frontend_bundle_asset().read_text(encoding="utf-8")
        template_source = (root / "backend" / "app" / "templates" / "index.html").read_text(encoding="utf-8")

        self.assertIn("cancelTask(taskId)", api_source)
        self.assertIn("async cancelTask()", app_source)
        self.assertIn('id="btn-cancel-task"', template_source)


if __name__ == "__main__":
    unittest.main()
