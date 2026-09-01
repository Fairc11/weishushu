"""运行时上下文契约测试。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.runtime_context import (
    PROFILE_DEV,
    PROFILE_USER,
    RUNTIME_FROZEN,
    RUNTIME_SOURCE,
    RuntimeContextError,
    resolve_runtime_context,
)

MANIFEST_NAME = "weishushu_build_manifest.json"


def _write_manifest(
    directory: Path,
    *,
    platform: str = "darwin",
    profile: str = "user",
    executable_name: str = "Weishushu",
    bundle_identifier: str = "com.weishushu.desktop",
    app_version: str = "2.0.0",
    source_commit: str = "abc123commit",
) -> Path:
    from packaging.build_manifest import make_manifest, write_manifest

    manifest = make_manifest(
        app_version=app_version,
        source_commit=source_commit,
        platform=platform,
        architecture="arm64",
        python_version="3.12.13",
        pyinstaller_version="6.0.0",
        dependency_lock_sha256="d" * 64,
        profile=profile,
        executable_name=executable_name,
        bundle_identifier=bundle_identifier,
        resources=[],
    )
    path = directory / MANIFEST_NAME
    write_manifest(path, manifest)
    return path


def _write_raw_manifest(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _mac_app_paths(tmp_path: Path, bundle_name: str) -> tuple[Path, Path]:
    executable = (
        tmp_path / f"{bundle_name}.app" / "Contents" / "MacOS" / bundle_name
    )
    resources = tmp_path / f"{bundle_name}.app" / "Contents" / "Resources"
    return executable, resources


def _patch_darwin_home(monkeypatch, home: Path) -> None:
    """模拟 darwin 的 frozen 测试显式替换 runtime_context 的家目录。"""
    monkeypatch.setattr(
        "backend.app.runtime_context.Path.home", lambda: home
    )

def test_source_context_is_dev_and_project_local() -> None:
    with patch.object(sys, "frozen", False, create=True), \
         patch.dict(os.environ, {}, clear=True):
        ctx = resolve_runtime_context()
        assert ctx.run_mode == RUNTIME_SOURCE
        assert ctx.profile == PROFILE_DEV
        assert ctx.platform == sys.platform
        assert ctx.data_root == Path(__file__).resolve().parents[1] / ".run"
        assert ctx.cookie_file.name == ".weibo_book_cookies_dev"
        assert ctx.self_test_root is None


def test_frozen_mac_dev_uses_executable_name(tmp_path: Path, monkeypatch) -> None:
    _patch_darwin_home(monkeypatch, tmp_path / "home")
    executable, resources = _mac_app_paths(tmp_path, "WeishushuDev")
    _write_manifest(
        resources,
        profile="dev",
        executable_name="WeishushuDev",
        bundle_identifier="com.weishushu.desktop.dev",
    )
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {"WEISHUSHU_PROFILE": "dev"}, clear=True):
        ctx = resolve_runtime_context()
        assert ctx.run_mode == RUNTIME_FROZEN
        assert ctx.profile == PROFILE_DEV
        assert ctx.executable_name == "WeishushuDev"
        assert ctx.source_commit == "abc123commit"
        assert ctx.manifest_path == resources / MANIFEST_NAME


def test_frozen_mac_user_ignores_external_dev_profile(tmp_path: Path, monkeypatch) -> None:
    _patch_darwin_home(monkeypatch, tmp_path / "home")
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_manifest(resources)
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {"WEISHUSHU_PROFILE": "dev"}, clear=True):
        ctx = resolve_runtime_context()
        assert ctx.profile == PROFILE_USER
        assert ctx.cookie_file.name == ".weibo_book_cookies"


def test_frozen_reads_app_version_and_commit_from_manifest(tmp_path: Path, monkeypatch) -> None:
    _patch_darwin_home(monkeypatch, tmp_path / "home")
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_manifest(
        resources,
        app_version="9.9.9",
        source_commit="fedcba9876",
    )
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {}, clear=True):
        ctx = resolve_runtime_context()
        assert ctx.app_version == "9.9.9"
        assert ctx.source_commit == "fedcba9876"


def test_frozen_missing_manifest_fails_closed(tmp_path: Path) -> None:
    executable, _resources = _mac_app_paths(tmp_path, "Weishushu")
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeContextError):
            resolve_runtime_context()


def test_frozen_manifest_unknown_commit_fails_closed(tmp_path: Path) -> None:
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_raw_manifest(
        resources,
        {
            "schema_version": 1,
            "app_version": "2.0.0",
            "source_commit": "unknown",
            "platform": "darwin",
            "architecture": "arm64",
            "python_version": "3.12.13",
            "pyinstaller_version": "6.0.0",
            "dependency_lock_sha256": "d" * 64,
            "profile": "user",
            "executable_name": "Weishushu",
            "bundle_identifier": "com.weishushu.desktop",
            "resources": [],
        },
    )
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeContextError):
            resolve_runtime_context()


def test_frozen_manifest_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    # 用户版可执行文件名配开发版清单：身份不一致必须失败。
    _write_manifest(
        resources,
        profile="dev",
        executable_name="WeishushuDev",
        bundle_identifier="com.weishushu.desktop.dev",
    )
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeContextError):
            resolve_runtime_context()


def test_source_mode_still_allows_unknown_commit() -> None:
    with patch.object(sys, "frozen", False, create=True), \
         patch.dict(os.environ, {}, clear=True):
        ctx = resolve_runtime_context()
        assert ctx.source_commit == "unknown"


def _forbid_home(monkeypatch) -> None:
    """把两个模块的 Path.home 换成一旦调用就抛异常的哨兵。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("自检模式不得读取 Path.home")

    monkeypatch.setattr("backend.app.runtime_context.Path.home", _boom)
    monkeypatch.setattr("backend.app.platform_paths.Path.home", _boom)


def test_self_test_root_never_reads_home_on_darwin(tmp_path, monkeypatch) -> None:
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_manifest(resources)
    root = tmp_path / "self-test-root"
    _forbid_home(monkeypatch)
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(
             os.environ,
             {"WEISHUSHU_SELF_TEST_ROOT": str(root)},
             clear=True,
         ):
        resolved = root.resolve()
        ctx = resolve_runtime_context()
        assert ctx.self_test_root == resolved
        assert ctx.data_root == resolved / "data"
        assert ctx.cache_root == resolved / "cache"
        assert ctx.log_root == resolved / "logs"
        assert ctx.state_root == resolved / "state"
        assert ctx.output_root == resolved / "output"
        assert ctx.cookie_file == resolved / "cookies.json"


def test_self_test_root_never_reads_home_on_windows(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "Weishushu" / "Weishushu.exe"
    internal = tmp_path / "Weishushu" / "_internal"
    _write_manifest(
        internal,
        platform="win32",
        executable_name="Weishushu.exe",
    )
    root = tmp_path / "self-test-root"
    _forbid_home(monkeypatch)
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "win32", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {"WEISHUSHU_SELF_TEST_ROOT": str(root)}, clear=True):
        ctx = resolve_runtime_context()
        resolved = root.resolve()
        assert ctx.data_root == resolved / "data"
        assert ctx.cookie_file == resolved / "cookies.json"


def test_frozen_windows_user_accepts_only_exact_exe(tmp_path: Path) -> None:
    executable_root = tmp_path / "Weishushu"
    executable = executable_root / "Weishushu.exe"
    internal = executable_root / "_internal"
    _write_manifest(
        internal,
        platform="win32",
        executable_name="Weishushu.exe",
    )
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "win32", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(
             os.environ,
             {"LOCALAPPDATA": str(tmp_path / "local"), "WEISHUSHU_PROFILE": "dev"},
             clear=True,
         ):
        ctx = resolve_runtime_context()
        assert ctx.profile == PROFILE_USER
        assert ctx.executable_name == "Weishushu.exe"
        assert ctx.source_commit == "abc123commit"


def test_frozen_windows_rejects_unknown_executable(tmp_path: Path) -> None:
    executable = tmp_path / "Other.exe"
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "win32", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeContextError):
            resolve_runtime_context()


def test_self_test_root_overrides_writable_paths(tmp_path: Path) -> None:
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_manifest(resources)
    root = tmp_path / "self-test-root"
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {"WEISHUSHU_SELF_TEST_ROOT": str(root)}, clear=True):
        resolved = root.resolve()
        ctx = resolve_runtime_context()
        assert ctx.self_test_root == resolved
        assert ctx.data_root == resolved / "data"
        assert ctx.cache_root == resolved / "cache"
        assert ctx.log_root == resolved / "logs"
        assert ctx.state_root == resolved / "state"
        assert ctx.output_root == resolved / "output"
        assert ctx.cookie_file == resolved / "cookies.json"


def test_source_rejects_self_test_root() -> None:
    with patch.object(sys, "frozen", False, create=True), \
         patch.dict(os.environ, {"WEISHUSHU_SELF_TEST_ROOT": "/tmp/x"}, clear=True):
        with pytest.raises(RuntimeContextError):
            resolve_runtime_context()


def test_old_path_modules_cannot_bypass_self_test_root(tmp_path: Path) -> None:
    """platform_paths/config/setup_check 在自检模式下必须全部落在自检根内。"""
    executable, resources = _mac_app_paths(tmp_path, "Weishushu")
    _write_manifest(resources)
    root = tmp_path / "self-test-root"
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "platform", "darwin", create=True), \
         patch.object(sys, "executable", str(executable)), \
         patch.dict(os.environ, {"WEISHUSHU_SELF_TEST_ROOT": str(root)}, clear=True):
        resolved = Path(root).resolve()

        from backend.app.platform_paths import PlatformPaths, cookie_file_candidates

        paths = PlatformPaths()
        assert paths.local_app_data_dir().is_relative_to(resolved)
        assert paths.log_dir().is_relative_to(resolved)
        assert paths.cache_dir().is_relative_to(resolved)
        assert paths.state_dir().is_relative_to(resolved)
        assert paths.output_dir().is_relative_to(resolved)
        assert all(c.is_relative_to(resolved) for c in cookie_file_candidates())

        from backend.app.config import Settings

        settings = Settings()
        assert settings.local_app_data_dir.is_relative_to(resolved)
        assert settings.log_dir.is_relative_to(resolved)
        assert settings.state_dir.is_relative_to(resolved)

        from backend.app.services.setup_check import get_localappdata_ms_playwright

        assert get_localappdata_ms_playwright().is_relative_to(resolved)



def test_runtime_context_is_immutable_dataclass() -> None:
    ctx = resolve_runtime_context()
    with pytest.raises(Exception):
        ctx.profile = "user"  # type: ignore[misc]
