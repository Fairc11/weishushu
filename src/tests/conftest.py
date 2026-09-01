from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolate_global_persistent_task_store(tmp_path):
    """每个用例使用独立的应用任务记录，不触碰开发态真实状态。"""
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import task_manager

    original = task_manager._persistent_store
    task_manager._persistent_store = PersistentTaskStore(
        tmp_path / "app-state" / "active-personal-archive-task.json"
    )
    try:
        yield
    finally:
        for handle in list(task_manager._gc_timers.values()):
            handle.cancel()
        task_manager._gc_timers.clear()
        task_manager._tasks.clear()
        task_manager._persistent_store = original
