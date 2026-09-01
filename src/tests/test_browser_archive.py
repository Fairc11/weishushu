"""浏览器归档/Windows浏览器树 Chromium 路径解析与清单契约测试。"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.write_build_manifest import (
    inspect_browser_archive,
    inspect_browser_tree,
)


def _archive(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "playwright-browsers.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_inspect_browser_archive_finds_chrome_headless_shell(tmp_path: Path) -> None:
    path = _archive(
        tmp_path,
        {
            "chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell": b"bin",
            "chromium_headless_shell-1217/readme.txt": b"readme",
        },
    )
    assert inspect_browser_archive(path) == (
        "chromium_headless_shell-1217/chrome-headless-shell-mac-arm64/chrome-headless-shell"
    )


def test_inspect_browser_archive_returns_none_when_no_chromium(tmp_path: Path) -> None:
    path = _archive(
        tmp_path,
        {
            "chromium_headless_shell-1217/readme.txt": b"readme",
        },
    )
    assert inspect_browser_archive(path) is None


def test_inspect_browser_archive_rejects_multiple_ambiguous_executables(
    tmp_path: Path,
) -> None:
    path = _archive(
        tmp_path,
        {
            "a/chrome-headless-shell": b"a",
            "b/chrome-headless-shell": b"b",
        },
    )
    with pytest.raises(ValueError, match="多个不明确"):
        inspect_browser_archive(path)


def test_write_build_manifest_uses_real_archive_member(tmp_path: Path, monkeypatch) -> None:
    import scripts.write_build_manifest as writer

    root = tmp_path / "root"
    (root / "backend/app/templates").mkdir(parents=True)
    (root / "backend/app/templates/index.html").write_text("index", encoding="utf-8")
    lock = tmp_path / "lock.txt"
    lock.write_text("a==1\n", encoding="utf-8")
    archive = _archive(
        tmp_path,
        {"chromium-123/chrome/chrome-headless-shell": b"bin"},
    )
    archive_target = root / "playwright-browsers.tar.gz"
    archive_target.write_bytes(archive.read_bytes())
    output = root / "manifest.json"
    monkeypatch.setattr(writer, "_git_source_commit", lambda: "abc123", raising=False)
    monkeypatch.setattr(writer, "_pyinstaller_version", lambda: "6.0.0", raising=False)
    assert writer.main([
        "--root", str(root),
        "--output", str(output),
        "--platform", "darwin",
        "--architecture", "arm64",
        "--profile", "user",
        "--executable-name", "Weishushu",
        "--bundle-id", "com.weishushu.desktop",
        "--dependency-lock", str(lock),
        "--resource-dir", "backend/app/templates",
        "--browser-archive", "playwright-browsers.tar.gz",
    ]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["browser_archive"]["path"] == "playwright-browsers.tar.gz"
    assert manifest["extracted_browser"]["location"] == "cache"
    assert manifest["extracted_browser"]["expected_relative_path"] == (
        "chromium-123/chrome/chrome-headless-shell"
    )


def _make_windows_tree(root: Path, files: dict[str, bytes]) -> Path:
    tree = root / "ms-playwright"
    for rel, data in files.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree


def test_inspect_browser_tree_finds_unique_chrome_headless_shell(
    tmp_path: Path,
) -> None:
    tree = _make_windows_tree(
        tmp_path,
        {
            "chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe": b"binary",
            "chromium_headless_shell-1217/readme.txt": b"readme",
        },
    )
    assert inspect_browser_tree(tree) == (
        "chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe"
    )


@pytest.mark.parametrize(
    "name",
    (
        "headless_shell.exe",
        "chromium-headless-shell.exe",
        "chrome.exe",
        "chromium.exe",
    ),
)
def test_inspect_browser_tree_accepts_fallback_names(
    tmp_path: Path, name: str
) -> None:
    tree = _make_windows_tree(tmp_path, {f"browser/{name}": b"binary"})
    assert inspect_browser_tree(tree) == f"browser/{name}"


def test_inspect_browser_tree_fails_when_no_executable(tmp_path: Path) -> None:
    tree = _make_windows_tree(
        tmp_path,
        {"chromium_headless_shell-1217/readme.txt": b"readme"},
    )
    with pytest.raises(ValueError, match="没有Chromium可执行文件"):
        inspect_browser_tree(tree)


def test_inspect_browser_tree_rejects_multiple_preferred_executables(
    tmp_path: Path,
) -> None:
    tree = _make_windows_tree(
        tmp_path,
        {
            "a/chrome-headless-shell.exe": b"a",
            "b/chrome-headless-shell.exe": b"b",
        },
    )
    with pytest.raises(ValueError, match="多个chrome-headless-shell.exe"):
        inspect_browser_tree(tree)


def test_browser_tree_and_browser_archive_are_mutually_exclusive(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.write_build_manifest as writer

    root = tmp_path / "root"
    (root / "backend/app/templates").mkdir(parents=True)
    (root / "backend/app/templates/index.html").write_text("index", encoding="utf-8")
    lock = tmp_path / "lock.txt"
    lock.write_text("a==1\n", encoding="utf-8")
    wheel = tmp_path / "archive.tar.gz"
    wheel.write_bytes(b"not-used")
    output = root / "manifest.json"
    monkeypatch.setattr(writer, "_git_source_commit", lambda: "abc123", raising=False)
    monkeypatch.setattr(writer, "_pyinstaller_version", lambda: "6.0.0", raising=False)
    assert writer.main([
        "--root", str(root),
        "--output", str(output),
        "--platform", "win32",
        "--architecture", "x64",
        "--profile", "user",
        "--executable-name", "Weishushu.exe",
        "--bundle-id", "com.weishushu.desktop",
        "--dependency-lock", str(lock),
        "--resource-dir", "backend/app/templates",
        "--browser-archive", str(wheel.relative_to(root)) if wheel.is_relative_to(root) else wheel.name,
        "--browser-tree", "ms-playwright",
    ]) == 1
    assert not output.exists()


def test_write_build_manifest_windows_bundle_has_location_and_resources(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.write_build_manifest as writer

    root = tmp_path / "root"
    _make_windows_tree(
        root,
        {
            "chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe": b"bin",
            "chromium_headless_shell-1217/readme.txt": b"readme",
            "chromium-1217/chrome-win64/chrome.exe": b"full",
        },
    )
    (root / "backend/app/templates").mkdir(parents=True)
    (root / "backend/app/templates/index.html").write_text("index", encoding="utf-8")
    lock = tmp_path / "lock.txt"
    lock.write_text("a==1\n", encoding="utf-8")
    output = root / "manifest.json"
    monkeypatch.setattr(writer, "_git_source_commit", lambda: "abc123", raising=False)
    monkeypatch.setattr(writer, "_pyinstaller_version", lambda: "6.0.0", raising=False)
    assert writer.main([
        "--root", str(root),
        "--output", str(output),
        "--platform", "win32",
        "--architecture", "x64",
        "--profile", "user",
        "--executable-name", "Weishushu.exe",
        "--bundle-id", "com.weishushu.desktop",
        "--dependency-lock", str(lock),
        "--resource-dir", "backend/app/templates",
        "--browser-tree", "ms-playwright",
    ]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["extracted_browser"]["location"] == "bundle"
    assert manifest["extracted_browser"]["expected_relative_path"] == (
        "chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe"
    )
    resource_paths = {item["path"] for item in manifest["resources"]}
    assert "ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-win64/chrome-headless-shell.exe" in resource_paths
    assert "ms-playwright/chromium_headless_shell-1217/readme.txt" in resource_paths
    exe_entry = next(
        item for item in manifest["resources"]
        if item["path"].endswith("chrome-headless-shell.exe")
    )
    assert exe_entry["sha256"] == hashlib.sha256(b"bin").hexdigest()
