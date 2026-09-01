"""最早期 PyInstaller runtime hook 测试。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_hook():
    path = Path(__file__).resolve().parents[1] / "packaging/pyinstaller/runtime_hook.py"
    spec = importlib.util.spec_from_file_location("weishushu_runtime_hook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def test_hook_replaces_none_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    hook.install_early_runtime()
    assert sys.stdout is not None
    assert sys.stderr is not None
    sys.stdout.write("ignored")
    sys.stdout.flush()
    sys.stderr.write("ignored")
    sys.stderr.flush()


def test_hook_cleans_frozen_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WEISHUSHU_PROFILE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PLAYWRIGHT_BROWSERS_PATH",
    ):
        monkeypatch.setenv(name, "should-be-removed")
    hook.install_early_runtime()
    for name in (
        "WEISHUSHU_PROFILE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PLAYWRIGHT_BROWSERS_PATH",
    ):
        assert name not in os.environ


def test_hook_does_not_import_business_modules() -> None:
    # hook 源码不得静态导入 FastAPI/pywebview/微博业务模块。
    source = Path(hook.__file__).read_text(encoding="utf-8")
    assert "import fastapi" not in source
    assert "import webview" not in source
    assert "from weibo_book" not in source
