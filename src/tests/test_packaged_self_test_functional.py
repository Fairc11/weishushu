"""功能自检骨架测试。"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.runtime_context import resolve_runtime_context
from desktop.self_test.functional import (
    FUNCTIONAL_STEPS,
    _resolve_browser_executable,
    run_functional_self_test,
)


def test_functional_self_test_writes_json_with_all_steps(tmp_path: Path) -> None:
    context = resolve_runtime_context()
    output = tmp_path / "result.json"
    result = run_functional_self_test(context, output)
    assert output.exists()
    assert {item["name"] for item in result["steps"]} == set(FUNCTIONAL_STEPS)
    assert result["error_kind"] is not None


def test_disk_json_matches_return_value_and_contains_final_error(tmp_path: Path) -> None:
    """最终 error_kind/message/步骤必须完整写入磁盘，不能被旧结果或半成品掩盖。"""
    context = resolve_runtime_context()
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_commit": "old",
                "profile": "user",
                "platform": "darwin",
                "steps": [{"name": "json_saved", "status": "passed", "message": ""}],
                "error_kind": None,
                "message": "",
                "log_path": "",
            }
        ),
        encoding="utf-8",
    )
    result = run_functional_self_test(context, output)
    disk = json.loads(output.read_text(encoding="utf-8"))
    assert disk == result
    assert "json_saved" in {item["name"] for item in disk["steps"]}
    assert disk["error_kind"] == result["error_kind"]
    assert disk["message"] == result["message"]


def test_offline_steps_pass_without_chromium_in_self_test_root(
    tmp_path: Path, monkeypatch
) -> None:
    """无 Chromium 时生产登录、媒体、档案离线步骤仍应分别通过。"""
    import os
    import sys
    from unittest.mock import patch

    from packaging.build_manifest import make_manifest, write_manifest

    bundle = tmp_path / "Weishushu.app"
    executable = bundle / "Contents" / "MacOS" / "Weishushu"
    resources = bundle / "Contents" / "Resources"
    write_manifest(
        resources / "weishushu_build_manifest.json",
        make_manifest(
            app_version="2.0.0",
            source_commit="offline123",
            platform="darwin",
            architecture="arm64",
            python_version="3.12.13",
            pyinstaller_version="6.0.0",
            dependency_lock_sha256="d" * 64,
            profile="user",
            executable_name="Weishushu",
            bundle_identifier="com.weishushu.desktop",
            resources=[],
        ),
    )
    root = tmp_path / "self-test-root"
    root.mkdir()
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable), create=True), \
         patch.object(sys, "_MEIPASS", str(Path(__file__).resolve().parents[1]), create=True), \
         patch.dict(os.environ, {"WEISHUSHU_SELF_TEST_ROOT": str(root)}, clear=True):
        from backend.app.runtime_context import resolve_runtime_context

        context = resolve_runtime_context()
        output = root / "result.json"
        result = run_functional_self_test(context, output)
        by_name = {item["name"]: item["status"] for item in result["steps"]}
        assert by_name["login_contract"] == "passed"
        assert by_name["media_download"] == "passed"
        assert by_name["archive_generate"] == "passed"

def _runtime_context_with_manifest(
    tmp_path: Path,
    manifest_path: Path,
    cache_root: Path | None = None,
):
    return types.SimpleNamespace(
        manifest_path=manifest_path,
        cache_root=cache_root or (tmp_path / "cache"),
    )


def _write_extracted_manifest(tmp_path: Path, extracted: dict | None) -> Path:
    from packaging.build_manifest import make_manifest, write_manifest

    manifest_path = tmp_path / "build-manifest.json"
    extra = {} if extracted is None else {"extracted_browser": extracted}
    write_manifest(
        manifest_path,
        make_manifest(
            app_version="2.0.0",
            source_commit="selftest123",
            platform="darwin",
            architecture="arm64",
            python_version="3.12.13",
            pyinstaller_version="6.0.0",
            dependency_lock_sha256="d" * 64,
            profile="user",
            executable_name="Weishushu",
            bundle_identifier="com.weishushu.desktop",
            resources=[],
            **extra,
        ),
    )
    return manifest_path


def test_resolve_browser_executable_cache_uses_cache_root(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    browser_root = cache_root / "ms-playwright" / "chromium-1217"
    executable = browser_root / "chrome-headless-shell.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bin")
    manifest_path = _write_extracted_manifest(
        tmp_path,
        {
            "location": "cache",
            "expected_relative_path": "chromium-1217/chrome-headless-shell.exe",
        },
    )
    context = _runtime_context_with_manifest(tmp_path, manifest_path, cache_root)
    assert _resolve_browser_executable(context) == executable.resolve()


def test_resolve_browser_executable_bundle_uses_frozen_root(tmp_path: Path) -> None:
    bundle_root = tmp_path / "internal" / "ms-playwright"
    executable = bundle_root / "chromium-1217" / "chrome-headless-shell.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bin")
    manifest_path = _write_extracted_manifest(
        tmp_path,
        {
            "location": "bundle",
            "expected_relative_path": "chromium-1217/chrome-headless-shell.exe",
        },
    )
    context = _runtime_context_with_manifest(tmp_path, manifest_path)
    with patch(
        "backend.app.services.setup_check.get_frozen_ms_playwright",
        return_value=bundle_root,
    ):
        assert _resolve_browser_executable(context) == executable.resolve()


def test_resolve_browser_executable_rejects_missing_extracted_browser(
    tmp_path: Path,
) -> None:
    manifest_path = _write_extracted_manifest(tmp_path, None)
    context = _runtime_context_with_manifest(tmp_path, manifest_path)
    with pytest.raises(ValueError, match="清单缺少"):
        _resolve_browser_executable(context)


def test_resolve_browser_executable_rejects_missing_bundle_file(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "internal" / "ms-playwright"
    bundle_root.mkdir(parents=True)
    manifest_path = _write_extracted_manifest(
        tmp_path,
        {
            "location": "bundle",
            "expected_relative_path": "chromium-1217/missing.exe",
        },
    )
    context = _runtime_context_with_manifest(tmp_path, manifest_path)
    with patch(
        "backend.app.services.setup_check.get_frozen_ms_playwright",
        return_value=bundle_root,
    ), pytest.raises(ValueError, match="清单 Chromium 不存在"):
        _resolve_browser_executable(context)


def test_resolve_browser_executable_rejects_missing_bundle_root(
    tmp_path: Path,
) -> None:
    manifest_path = _write_extracted_manifest(
        tmp_path,
        {
            "location": "bundle",
            "expected_relative_path": "chromium-1217/chrome-headless-shell.exe",
        },
    )
    context = _runtime_context_with_manifest(tmp_path, manifest_path)
    with patch(
        "backend.app.services.setup_check.get_frozen_ms_playwright",
        return_value=tmp_path / "missing-bundle",
    ), pytest.raises(ValueError, match="浏览器根不存在"):
        _resolve_browser_executable(context)
