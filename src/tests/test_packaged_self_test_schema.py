"""自检 schema 测试。"""

from __future__ import annotations

import json
from pathlib import Path

from desktop.self_test.schema import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    add_step,
    make_result_missing,
    new_result,
    step,
    write_result,
)


def test_new_result_has_schema_and_empty_steps() -> None:
    result = new_result(build_commit="abc", profile="user", platform="darwin")
    assert result["schema_version"] == 1
    assert result["build_commit"] == "abc"
    assert result["steps"] == []
    assert result["error_kind"] is None


def test_step_statuses_are_restricted() -> None:
    assert step("a", STATUS_PASSED)["status"] == "passed"
    assert step("a", STATUS_FAILED)["status"] == "failed"
    assert step("a", STATUS_SKIPPED, skip_reason="environment_unavailable")["skip_reason"] == "environment_unavailable"


def test_write_result_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "out.json"
    result = new_result(build_commit="abc", profile="user", platform="darwin")
    add_step(result, step("x", STATUS_PASSED))
    write_result(path, result)
    assert json.loads(path.read_text(encoding="utf-8"))["steps"][0]["status"] == "passed"


def test_result_missing_schema() -> None:
    result = make_result_missing(build_commit="x", profile="y", platform="z", message="missing")
    assert result["error_kind"] == "result_missing"
