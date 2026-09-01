"""构建清单 schema 与哈希验证测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from packaging.build_manifest import (
    BuildManifestError,
    collect_resource_paths,
    generate_manifest_from_tree,
    read_manifest,
    validate_manifest,
    verify_resources,
    write_manifest,
)


def _write(path: Path, text: str = "data\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_package_tree(root: Path) -> None:
    """模拟真实封包资源布局，不包含 run.py/backend/app/main.py 等源码文件。"""
    _write(root / "backend" / "app" / "templates" / "index.html")
    for name in (
        "tokens.css",
        "base.css",
        "shell.css",
        "components.css",
        "workflows.css",
        "responsive.css",
    ):
        _write(root / "backend" / "app" / "static" / "css" / name)
    _write(root / "weibo_book" / "templates" / "book_interactive.html")
    _write(root / "playwright-browsers.tar.gz")


def _manifest(root: Path) -> dict:
    resource_paths = collect_resource_paths(
        root,
        [
            "backend/app/templates",
            "backend/app/static",
            "weibo_book/templates",
        ],
    )
    resource_paths.append("playwright-browsers.tar.gz")
    return generate_manifest_from_tree(
        root,
        app_version="2.0.0",
        source_commit="abc123",
        platform="darwin",
        architecture="arm64",
        python_version="3.12.13",
        pyinstaller_version="6.0.0",
        dependency_lock_sha256="lockhash",
        profile="user",
        executable_name="Weishushu",
        bundle_identifier="com.weishushu.desktop",
        resource_paths=resource_paths,
        browser_archive={
            "path": "playwright-browsers.tar.gz",
            "sha256": "archivehash",
        },
        extracted_browser={"expected_relative_path": "chromium/chrome"},
    )


def test_generate_and_write_manifest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    path = root / "weishushu_build_manifest.json"
    write_manifest(path, manifest)

    loaded = read_manifest(path)
    assert loaded["schema_version"] == 1
    assert loaded["profile"] == "user"
    assert loaded["executable_name"] == "Weishushu"
    assert len(loaded["resources"]) == 9
    paths = {item["path"] for item in loaded["resources"]}
    assert "run.py" not in paths
    assert "backend/app/main.py" not in paths
    assert "backend/app/templates/index.html" in paths
    assert "backend/app/static/css/app.css" not in paths
    for name in (
        "tokens.css",
        "base.css",
        "shell.css",
        "components.css",
        "workflows.css",
        "responsive.css",
    ):
        assert f"backend/app/static/css/{name}" in paths
    assert "weibo_book/templates/book_interactive.html" in paths
    assert "playwright-browsers.tar.gz" in paths


def test_verify_resources_detects_tamper(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    assert verify_resources(root, manifest) == []

    (root / "backend/app/templates/index.html").write_text("changed\n", encoding="utf-8")
    errors = verify_resources(root, manifest)
    assert any("index.html" in error for error in errors)


def test_manifest_rejects_unknown_schema(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    manifest["schema_version"] = 99
    with pytest.raises(BuildManifestError, match="schema"):
        validate_manifest(manifest)


def test_manifest_rejects_unknown_source_or_pyinstaller(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    manifest["source_commit"] = "unknown"
    with pytest.raises(BuildManifestError, match="source_commit"):
        validate_manifest(manifest)
    manifest = _manifest(root)
    manifest["pyinstaller_version"] = "unknown"
    with pytest.raises(BuildManifestError, match="pyinstaller_version"):
        validate_manifest(manifest)


def test_manifest_rejects_bad_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    manifest["resources"][0]["path"] = "../escape"
    with pytest.raises(BuildManifestError, match="非法"):
        validate_manifest(manifest)
    manifest["resources"][0]["path"] = "a\\b"
    with pytest.raises(BuildManifestError, match="非法"):
        validate_manifest(manifest)


def test_manifest_rejects_duplicate_resources(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    manifest["resources"].append(dict(manifest["resources"][0]))
    with pytest.raises(BuildManifestError, match="重复"):
        validate_manifest(manifest)


def test_manifest_rejects_missing_required_field(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_package_tree(root)
    manifest = _manifest(root)
    del manifest["source_commit"]
    with pytest.raises(BuildManifestError, match="source_commit"):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    ("profile", "executable_name", "bundle_identifier", "platform"),
    [
        ("dev", "Weishushu", "com.weishushu.desktop.dev", "darwin"),
        ("user", "WeishushuDev", "com.weishushu.desktop", "darwin"),
        ("user", "Weishushu.exe", "com.weishushu.desktop", "darwin"),
        ("dev", "WeishushuDev.exe", "com.weishushu.desktop.dev", "win32"),
    ],
)
def test_manifest_rejects_identity_mismatch(
    profile, executable_name, bundle_identifier, platform
) -> None:
    manifest = {
        "schema_version": 1,
        "app_version": "2.0.0",
        "source_commit": "abc",
        "platform": platform,
        "architecture": "arm64",
        "python_version": "3.12.13",
        "pyinstaller_version": "6.0.0",
        "dependency_lock_sha256": "x",
        "profile": profile,
        "executable_name": executable_name,
        "bundle_identifier": bundle_identifier,
        "resources": [],
    }
    with pytest.raises(BuildManifestError, match="profile"):
        validate_manifest(manifest)


def test_manifest_accepts_matching_identity() -> None:
    manifest = {
        "schema_version": 1,
        "app_version": "2.0.0",
        "source_commit": "abc",
        "platform": "win32",
        "architecture": "x64",
        "python_version": "3.12.13",
        "pyinstaller_version": "6.0.0",
        "dependency_lock_sha256": "x",
        "profile": "user",
        "executable_name": "Weishushu.exe",
        "bundle_identifier": "com.weishushu.desktop",
        "resources": [],
    }
    validate_manifest(manifest)
    assert json.loads(json.dumps(manifest))["profile"] == "user"

def _extracted_manifest(
    *,
    platform: str = "darwin",
    browser_archive: dict | None = None,
    extracted_browser: dict,
) -> dict:
    is_win = platform == "win32"
    manifest = {
        "schema_version": 1,
        "app_version": "2.0.0",
        "source_commit": "abc",
        "platform": platform,
        "architecture": "arm64",
        "python_version": "3.12.13",
        "pyinstaller_version": "6.0.0",
        "dependency_lock_sha256": "x",
        "profile": "user",
        "executable_name": "Weishushu.exe" if is_win else "Weishushu",
        "bundle_identifier": "com.weishushu.desktop",
        "resources": [],
    }
    if browser_archive is not None:
        manifest["browser_archive"] = browser_archive
    manifest["extracted_browser"] = extracted_browser
    return manifest


def test_manifest_rejects_illegal_extracted_browser_path() -> None:
    for bad in ("../escape", "a\\b", "/abs"):
        manifest = _extracted_manifest(
            platform="darwin",
            browser_archive={"path": "archive.tar.gz"},
            extracted_browser={
                "location": "cache",
                "expected_relative_path": bad,
            },
        )
        with pytest.raises(BuildManifestError, match="非法"):
            validate_manifest(manifest)


def test_manifest_rejects_browser_archive_with_bundle_location() -> None:
    manifest = _extracted_manifest(
        browser_archive={"path": "archive.tar.gz", "sha256": "x"},
        extracted_browser={
            "location": "bundle",
            "expected_relative_path": "chromium/chrome.exe",
        },
    )
    with pytest.raises(BuildManifestError, match="必须为 cache"):
        validate_manifest(manifest)


def test_manifest_rejects_win32_without_archive_using_cache() -> None:
    manifest = _extracted_manifest(
        platform="win32",
        extracted_browser={
            "location": "cache",
            "expected_relative_path": "chromium/chrome.exe",
        },
    )
    with pytest.raises(BuildManifestError, match="必须为 bundle"):
        validate_manifest(manifest)


def test_old_mac_manifest_missing_location_treated_as_cache() -> None:
    manifest = _extracted_manifest(
        browser_archive={"path": "archive.tar.gz", "sha256": "x"},
        extracted_browser={"expected_relative_path": "chromium/chrome"},
    )
    validate_manifest(manifest)
