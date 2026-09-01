"""macOS 原生关闭在持久任务运行时的保护契约。"""

import asyncio
import threading

import pytest

from test_focused_workflow import app_harness, run_node


@pytest.mark.parametrize("state", ["running", "pausing", "cancelling"])
def test_active_persistent_task_blocks_native_close_and_shows_frontend(tmp_path, state):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from desktop_app import DesktopCloseProtection
    from js_api import JsApi

    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    task_id = asyncio.run(manager.create_personal_archive(
        mode="incremental",
        output_dir=str((tmp_path / "微博书").resolve()),
    ))
    record = manager.get(task_id)
    assert record is not None
    record.state = state
    manager._persist(record, state=state)
    callback_returned = threading.Event()
    javascript_started = threading.Event()

    def evaluate_js(_window, value):
        javascript_started.set()
        assert callback_returned.wait(timeout=1), "closing 回调返回前不得同步执行 JavaScript"
        calls.append(value)

    window = type("Window", (), {"evaluate_js": evaluate_js})()
    calls: list[str] = []
    api = JsApi()
    api.set_window(window)

    allowed = DesktopCloseProtection(manager, api).handle_closing()
    callback_returned.set()

    assert allowed is False
    assert javascript_started.wait(timeout=1)
    for thread in threading.enumerate():
        if thread.name == "weishushu-close-protection":
            thread.join(timeout=1)
    assert calls == ["Ptu.showCloseProtection()"]


def test_waiting_task_allows_native_close(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from desktop_app import DesktopCloseProtection
    from js_api import JsApi

    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    task_id = asyncio.run(manager.create_personal_archive(
        mode="create",
        output_dir=str((tmp_path / "微博书").resolve()),
    ))
    record = manager.get(task_id)
    assert record is not None
    record.state = "waiting_resume"
    manager._persist(record, state="waiting_resume")

    assert DesktopCloseProtection(manager, JsApi()).handle_closing() is True


def test_close_after_pause_requires_matching_waiting_task_and_allows_one_close(tmp_path):
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from js_api import JsApi

    manager = TaskManager(PersistentTaskStore(tmp_path / "task.json"))
    task_id = asyncio.run(manager.create_personal_archive(
        mode="create",
        output_dir=str((tmp_path / "微博书").resolve()),
    ))
    destroyed: list[bool] = []
    window = type("Window", (), {"destroy": lambda self: destroyed.append(True)})()
    api = JsApi(task_manager=manager)
    api.set_window(window)

    assert api.close_after_pause(task_id) is False
    record = manager.get(task_id)
    assert record is not None
    record.state = "waiting_resume"
    manager._persist(record, state="waiting_resume")
    assert api.close_after_pause(task_id) is True
    assert destroyed == [True]
    assert api.consume_close_permission() is True
    assert api.consume_close_permission() is False


def test_close_protection_frontend_pauses_to_safe_point_before_destroying():
    run_node(app_harness("""
Ptu.State.taskId = '0123456789ab';
let pauseCalls = 0;
let closeCalls = 0;
Ptu.Api.pauseTask = async (taskId) => { pauseCalls += 1; return { task_id:taskId, state:'pausing' }; };
Ptu.Api.taskStatus = async () => ({ state:'waiting_resume' });
Ptu.Api.closeAfterPause = async (taskId) => { if (taskId !== Ptu.State.taskId) throw new Error('任务标识错误'); closeCalls += 1; return true; };
Ptu.showCloseProtection();
if (element('close-protection-overlay').hidden) throw new Error('关闭保护未显示');
(async () => {
  await Ptu.pauseThenClose();
  if (pauseCalls !== 1 || closeCalls !== 1) throw new Error('未在暂停后关闭');
})().catch((error) => { console.error(error); process.exit(1); });
"""))
