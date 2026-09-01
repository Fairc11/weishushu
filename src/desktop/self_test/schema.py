"""自检 JSON schema、步骤状态和退出码。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ENVIRONMENT_UNAVAILABLE = 3
ERROR_KIND_ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
ERROR_KIND_RESULT_MISSING = "result_missing"
ERROR_KIND_PROCESS_FAILED = "process_failed"
ERROR_KIND_STEP_FAILED = "step_failed"


def new_result(
    *,
    build_commit: str,
    profile: str,
    platform: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "build_commit": build_commit,
        "profile": profile,
        "platform": platform,
        "steps": [],
        "error_kind": None,
        "message": "",
        "log_path": "",
    }


def step(
    name: str,
    status: str,
    *,
    message: str = "",
    skip_reason: str | None = None,
) -> dict[str, Any]:
    if status not in (STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED):
        raise ValueError(f"非法步骤状态: {status}")
    item: dict[str, Any] = {"name": name, "status": status, "message": message}
    if skip_reason is not None:
        item["skip_reason"] = skip_reason
    return item


def add_step(result: dict[str, Any], item: dict[str, Any]) -> None:
    result["steps"].append(item)


def set_error(result: dict[str, Any], kind: str, message: str) -> None:
    result["error_kind"] = kind
    result["message"] = message


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace 原子写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_result(path: Path, result: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def load_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_result_missing(
    *,
    build_commit: str,
    profile: str,
    platform: str,
    message: str,
    log_path: str = "",
) -> dict[str, Any]:
    result = new_result(build_commit=build_commit, profile=profile, platform=platform)
    result["error_kind"] = ERROR_KIND_RESULT_MISSING
    result["message"] = message
    result["log_path"] = log_path
    return result
