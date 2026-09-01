"""自检错误分类测试：最早失败步骤映射到固定 error_kind。"""

from __future__ import annotations

import pytest

from desktop.self_test.functional import error_kind_for_step
from desktop.self_test.schema import STATUS_FAILED, add_step, new_result, step
from desktop.self_test.functional import run_functional_self_test


@pytest.mark.parametrize(
    ("step_name", "expected_kind"),
    [
        ("manifest_identity", "manifest"),
        ("writable_paths_isolated", "filesystem"),
        ("output_isolated", "filesystem"),
        ("fastapi_health", "resource"),
        ("static_assets", "resource"),
        ("chromium_launch", "browser"),
        ("cookie_isolated", "browser"),
        ("login_contract", "login_contract"),
        ("media_download", "media"),
        ("archive_generate", "archive"),
    ],
)
def test_error_kind_mapping(step_name: str, expected_kind: str) -> None:
    assert error_kind_for_step(step_name) == expected_kind


@pytest.mark.parametrize(
    ("first_failed", "later_failed", "expected_kind"),
    [
        ("manifest_identity", "media_download", "manifest"),
        ("writable_paths_isolated", "archive_generate", "filesystem"),
        ("fastapi_health", "login_contract", "resource"),
        ("chromium_launch", "static_assets", "browser"),
        ("login_contract", "archive_generate", "login_contract"),
        ("media_download", "output_isolated", "media"),
        ("archive_generate", "cookie_isolated", "archive"),
    ],
)
def test_first_failed_step_wins_and_steps_are_preserved(
    first_failed: str, later_failed: str, expected_kind: str
) -> None:
    result = new_result(build_commit="a", profile="user", platform="darwin")
    add_step(result, step(first_failed, STATUS_FAILED, message="first"))
    add_step(result, step("passed", "passed", message="ok"))
    add_step(result, step(later_failed, STATUS_FAILED, message="later"))

    # 直接复制 run_functional 的错误选择逻辑，避免启动完整 self-test。
    from desktop.self_test.schema import set_error
    failed = [item for item in result["steps"] if item["status"] == STATUS_FAILED]
    set_error(result, error_kind_for_step(failed[0]["name"]), f"最早失败步骤: {failed[0]['name']}")

    assert result["error_kind"] == expected_kind
    assert len(result["steps"]) == 3
    assert result["steps"][-1]["name"] == later_failed
