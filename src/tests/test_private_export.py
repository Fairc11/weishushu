"""私有构建镜像确定性导出测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import scripts.create_private_export as private_export
from scripts.create_private_export import (
    PRIVATE_DIRECTORIES,
    PRIVATE_MAPPINGS,
    PRIVATE_ROOT_FILES,
    PRIVATE_SCRIPT_FILES,
    PrivateExportError,
    _export,
)


from packaging.private.verify_shared_sources import verify


WORKFLOW_TARGET = ".github/workflows/ci.yml"
GITATTRIBUTES_SOURCE = "packaging/private/gitattributes"
GITATTRIBUTES_TARGET = ".gitattributes"
EXACT_GITATTRIBUTES = b"* text=auto eol=lf\n"

VALID_CI_YAML = (
    "name: private windows ci\n"
    "on:\n"
    "  pull_request:\n"
    "  workflow_dispatch:\n"
    "permissions:\n"
    "  contents: read\n"
    "jobs:\n"
    "  verify-and-build:\n"
    "    runs-on: windows-2025\n"
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
)


def _write(path: Path, text: str = "safe\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative in PRIVATE_ROOT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".ico":
            path.write_bytes(b"\x00\x00\x01\x00")
        elif relative == ".gitignore":
            # 不得写出自匹配内容；否则add -A时.gitignore被自身规则排除。
            path.write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
        elif relative == "pytest.ini":
            # 必须为合法ini；否则导出树内pytest无法收集任何测试。
            path.write_text(
                "[pytest]\nasyncio_default_fixture_loop_scope = function\n",
                encoding="utf-8",
            )
        else:
            path.write_text(f"{relative}\n", encoding="utf-8")
    for directory in PRIVATE_DIRECTORIES:
        _write(root / directory / "kept.txt")
    for relative in PRIVATE_SCRIPT_FILES:
        _write(root / "scripts" / relative)
    for source_relative, _target in PRIVATE_MAPPINGS.items():
        if source_relative == "packaging/private/ci.yml":
            _write(root / source_relative, VALID_CI_YAML)
        elif source_relative == GITATTRIBUTES_SOURCE:
            path = root / source_relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(EXACT_GITATTRIBUTES)
        else:
            _write(root / source_relative, f"mapping:{source_relative}\n")
    _write(root / "docs" / "README.md")


def test_private_export_whitelists_are_explicit() -> None:
    assert ".git" not in PRIVATE_ROOT_FILES
    assert "dist" not in PRIVATE_DIRECTORIES
    assert "build" not in PRIVATE_DIRECTORIES
    assert "releases" not in PRIVATE_DIRECTORIES
    assert "installer" not in PRIVATE_DIRECTORIES
    assert private_export.MANIFEST_NAME == "shared-source-manifest.json"


def test_private_export_uses_whitelist_and_mappings(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _write(source / "UNLISTED.txt")

    manifest = _export(source, target)

    assert manifest["schema_version"] == 1
    assert manifest["source_commit"] == "unknown"
    assert not (target / "UNLISTED.txt").exists()
    assert (target / "run.py").is_file()
    for source_rel, target_rel in PRIVATE_MAPPINGS.items():
        if source_rel == "packaging/private/ci.yml":
            expected = VALID_CI_YAML
        elif source_rel == GITATTRIBUTES_SOURCE:
            expected = EXACT_GITATTRIBUTES.decode("utf-8")
        else:
            expected = f"mapping:{source_rel}\n"
        assert (target / target_rel).is_file()
        assert (target / target_rel).read_text(encoding="utf-8") == expected
    assert (target / "shared-source-manifest.json").is_file()
    exported = json.loads((target / "shared-source-manifest.json").read_text(encoding="utf-8"))
    paths = {item["path"] for item in exported["files"]}
    assert "run.py" in paths
    assert ".github/workflows/ci.yml" in paths
    assert "tests/test_ci_workflow.py" in paths
    assert "scripts/verify_shared_sources.py" in paths


def test_private_export_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target1 = tmp_path / "target1"
    target2 = tmp_path / "target2"
    _make_source(source)
    os.environ["WEISHUSHU_EXPORT_SOURCE_COMMIT"] = "deadbeef"
    try:
        _export(source, target1)
        _export(source, target2)
    finally:
        os.environ.pop("WEISHUSHU_EXPORT_SOURCE_COMMIT", None)

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "shared-source-manifest.json"
        }

    assert snapshot(target1) == snapshot(target2)
    assert (target1 / "shared-source-manifest.json").read_bytes() == (
        target2 / "shared-source-manifest.json"
    ).read_bytes()


def test_private_export_allows_pinned_self_test_cookie_fixture(tmp_path: Path) -> None:
    """阶段2离线自检fixture是假Cookie，按精确路径+哈希放行进私有镜像。"""
    real_fixture = (
        Path(__file__).resolve().parents[1]
        / "desktop" / "self_test" / "fixtures" / "cookies.json"
    )
    source = tmp_path / "source"
    _make_source(source)
    fixture_rel = Path("desktop") / "self_test" / "fixtures" / "cookies.json"
    (source / fixture_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / fixture_rel).write_bytes(real_fixture.read_bytes())

    target = tmp_path / "target"
    manifest = _export(source, target)

    paths = {item["path"] for item in manifest["files"]}
    assert fixture_rel.as_posix() in paths
    assert (target / fixture_rel).read_bytes() == real_fixture.read_bytes()


def test_private_export_rejects_cookies_json_outside_fixture_allowlist(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "backend" / "cookies.json")

    with pytest.raises(PrivateExportError, match="禁止"):
        _export(source, tmp_path / "target")


def test_private_export_rejects_tampered_self_test_cookie_fixture(
    tmp_path: Path,
) -> None:
    """放行路径的内容哈希与锁定值不一致时必须拒绝，防止真实Cookie借道。"""
    source = tmp_path / "source"
    _make_source(source)
    fixture_rel = Path("desktop") / "self_test" / "fixtures" / "cookies.json"
    (source / fixture_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / fixture_rel).write_text(
        '[{"name": "SUB", "value": "REAL-COOKIE", "domain": ".weibo.cn", "path": "/"}]\n',
        encoding="utf-8",
    )

    with pytest.raises(PrivateExportError, match="哈希不一致"):
        _export(source, tmp_path / "target")


def test_private_export_includes_public_export_script(tmp_path: Path) -> None:
    """release_check 第32项需要读取 scripts/create_public_export.py。"""
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "scripts" / "create_public_export.py")

    target = tmp_path / "target"
    manifest = _export(source, target)

    paths = {item["path"] for item in manifest["files"]}
    assert "scripts/create_public_export.py" in paths
    assert (target / "scripts" / "create_public_export.py").is_file()


def test_check32_passes_inside_private_export_tree(tmp_path: Path) -> None:
    """对私有导出树执行 release_check 第32项必须通过。"""
    from scripts import release_check

    source = tmp_path / "source"
    _make_source(source)
    _write(
        source / "js_api.py",
        "from backend.app.platform_paths import cookie_file_candidates\n",
    )
    _write(
        source / "backend" / "app" / "platform_paths.py",
        "from weibo_book.login import get_cookie_file_path\n"
        "def cookie_file_candidates():\n"
        "    raise NotImplementedError\n",
    )
    _write(
        source / "weibo_book" / "login.py",
        'DEFAULT_COOKIE_FILE = ".weibo_book_cookies"\n',
    )
    _write(source / ".gitignore", ".weibo_book_cookies\n.weibo_book_cookies_dev\n")
    _write(
        source / "scripts" / "create_public_export.py",
        'SENSITIVE_LOGIN_FILE_NAMES = {".weibo_book_cookies", ".weibo_book_cookies_dev"}\n',
    )

    target = tmp_path / "target"
    _export(source, target)

    with patch("scripts.release_check.ROOT", target):
        result = release_check.check_32_jsapi_cookie_path_unified()
    assert result.ok, result.msg


def test_real_cookie_boundary_protections_stay_in_place() -> None:
    """两种Cookie文件必须同时受 .gitignore、公开导出与私有导出门禁保护。"""
    root = Path(__file__).resolve().parents[1]
    required = {".weibo_book_cookies", ".weibo_book_cookies_dev"}

    gitignore = {
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert required <= gitignore

    export_text = (root / "scripts" / "create_public_export.py").read_text(encoding="utf-8")
    for name in required:
        assert name in export_text

    assert required <= set(private_export.FORBIDDEN_FILE_NAMES)


def test_private_export_rejects_forbidden_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    forbidden_unlisted = [
        ".git/config",
        "dist/app.exe",
        "build/app.exe",
        "releases/app.exe",
        "installer/app.exe",
        "output/book.html",
        "个人数据/cookie.txt",
        ".weibo_book_cookies",
        "data/archive.db",
        "logs/boot.log",
    ]
    for relative in forbidden_unlisted:
        _write(source / relative)

    target = tmp_path / "target"
    _export(source, target)
    for relative in forbidden_unlisted:
        assert not (target / relative).exists(), relative

    # 白名单目录内的禁止文件必须拒绝导出。
    _write(source / "docs" / "private.db")
    with pytest.raises(PrivateExportError, match="禁止"):
        _export(source, tmp_path / "target2")


def test_private_export_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    (source / "backend" / "linked.txt").symlink_to(source / "README.md")

    with pytest.raises(PrivateExportError, match="符号链接"):
        _export(source, tmp_path / "target")


def test_private_export_rejects_nonempty_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(tmp_path / "target" / "existing.txt")

    with pytest.raises(PrivateExportError, match="不为空"):
        _export(source, tmp_path / "target")


def test_private_export_rejects_missing_whitelist(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    (source / "run.py").unlink()

    with pytest.raises(PrivateExportError, match="run.py"):
        _export(source, tmp_path / "target")


def test_verify_shared_sources_accepts_export(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)

    assert verify(target) == []


def test_verify_shared_sources_rejects_tamper(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)
    (target / "run.py").write_text("tampered\n", encoding="utf-8")

    errors = verify(target)
    assert any("run.py" in error for error in errors)


def test_verify_shared_sources_rejects_extra_and_missing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)
    (target / "extra.txt").write_text("extra\n", encoding="utf-8")
    (target / "LICENSE").unlink()

    errors = verify(target)
    assert any("extra.txt" in error for error in errors)
    assert any("LICENSE" in error for error in errors)


def test_verify_shared_sources_rejects_bad_manifest_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)
    manifest_path = target / "shared-source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = verify(target)
    assert any("非法路径" in error for error in errors)


def test_verify_tolerates_real_checkout_git_directory(tmp_path: Path) -> None:
    """真实 checkout 形态的 .git 目录必须被精确忽略，其余照常校验。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)

    git_dir = target / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    objects = git_dir / "objects" / "ab"
    objects.mkdir(parents=True)
    (objects / "cdef0123").write_bytes(b"\x00\x01")

    assert verify(target) == []


def test_verify_tolerates_git_worktree_pointer_file(tmp_path: Path) -> None:
    """worktree 形态下 .git 是一个指针文件，同样必须被精确忽略。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)

    (target / ".git").write_text("gitdir: /elsewhere/repo/.git/worktrees/x\n", encoding="utf-8")

    assert verify(target) == []


def test_verify_rejects_ds_store_and_plain_extra_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_source(source)
    _export(source, target)

    (target / ".DS_Store").write_bytes(b"\x00\x00\x00\x01")
    (target / "extra.txt").write_text("stray\n", encoding="utf-8")

    errors = verify(target)
    assert any(".DS_Store" in error for error in errors)
    assert any("extra.txt" in error for error in errors)


def test_private_ci_mapping_has_required_stage0_steps() -> None:
    ci = (Path(__file__).resolve().parents[1] / "packaging/private/ci.yml").read_text(encoding="utf-8")
    assert "verify_shared_sources.py" in ci
    assert "python scripts/release_check.py" in ci
    assert "pytest tests/ -q" not in ci
    assert "release_check.py" in ci
    assert "compileall" in ci
    assert "workflow_dispatch" in ci
    assert "windows-2025" in ci
    assert "macos-" not in ci
    assert "dist/**" not in ci
    assert "retention-days: 3" in ci
    for path in (
        "installer/**/*.exe",
        "dist/Weishushu/_internal/weishushu_build_manifest.json",
        "${{ runner.temp }}/self-test.json",
        "${{ runner.temp }}/shell.json",
        "${{ runner.temp }}/installed-self-test.json",
    ):
        assert path in ci


# 以下为发布前CI工作流门禁测试：复刻Run 32575588446的here-string顶格失败形态。

BROKEN_HERE_STRING_YAML = (
    "name: private windows ci\n"
    "on:\n"
    "  pull_request:\n"
    "  workflow_dispatch:\n"
    "jobs:\n"
    "  verify-and-build:\n"
    "    runs-on: windows-2025\n"
    "    steps:\n"
    "      - name: Assert Python runtime identity\n"
    "        shell: pwsh\n"
    "        run: |\n"
    "          $code = @\"\n"
    "import sys\n"
    "import platform\n"
    "\"@\n"
    "          python -c $code\n"
)

MISSING_DISPATCH_YAML = VALID_CI_YAML.replace("  workflow_dispatch:\n", "")

PUSH_TRIGGER_YAML = VALID_CI_YAML.replace(
    "on:\n",
    "on:\n  push:\n",
)

MISSING_JOB_YAML = VALID_CI_YAML.replace(
    "  verify-and-build:\n",
    "  other-job:\n",
)

SCALAR_STEPS_YAML = VALID_CI_YAML.replace(
    "    steps:\n      - uses: actions/checkout@v4\n",
    "    steps: checkout-only\n",
)


def _with_extra_trigger(trigger_line: str) -> str:
    return VALID_CI_YAML.replace("on:\n", f"on:\n{trigger_line}")


def test_valid_ci_yaml_exports_successfully(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"

    manifest = _export(source, target)

    paths = {item["path"] for item in manifest["files"]}
    assert WORKFLOW_TARGET in paths
    assert "scripts/verify_windows_runtime.py" in paths
    assert (target / WORKFLOW_TARGET).is_file()
    assert (target / "scripts" / "verify_windows_runtime.py").is_file()


def test_invalid_here_string_yaml_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "packaging/private/ci.yml", BROKEN_HERE_STRING_YAML)

    with pytest.raises(PrivateExportError, match="YAML"):
        _export(source, tmp_path / "target")


def test_missing_workflow_dispatch_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "packaging/private/ci.yml", MISSING_DISPATCH_YAML)

    with pytest.raises(PrivateExportError, match="workflow_dispatch"):
        _export(source, tmp_path / "target")


def test_push_trigger_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "packaging/private/ci.yml", PUSH_TRIGGER_YAML)

    with pytest.raises(PrivateExportError, match="push"):
        _export(source, tmp_path / "target")


def test_schedule_trigger_rejected(tmp_path: Path) -> None:
    """反例：加入schedule即偏离精确触发器集合，导出必须失败。"""
    source = tmp_path / "source"
    _make_source(source)
    _write(
        source / "packaging/private/ci.yml",
        _with_extra_trigger("  schedule:\n    - cron: '0 3 * * *'\n"),
    )

    with pytest.raises(PrivateExportError, match="schedule"):
        _export(source, tmp_path / "target")


def test_any_extra_trigger_rejected_not_blacklist(tmp_path: Path) -> None:
    """repository_dispatch与workflow_call等任何额外触发器一律拒绝。"""
    for trigger_line in (
        "  repository_dispatch:\n",
        "  workflow_call:\n",
    ):
        source = tmp_path / f"source-{trigger_line.strip().rstrip(':')}"
        _make_source(source)
        _write(
            source / "packaging/private/ci.yml",
            _with_extra_trigger(trigger_line),
        )
        name = trigger_line.strip().rstrip(":").lstrip()
        with pytest.raises(PrivateExportError, match=name):
            _export(source, tmp_path / f"target-{name}")


def test_broken_jobs_steps_structure_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    _write(source / "packaging/private/ci.yml", MISSING_JOB_YAML)

    with pytest.raises(PrivateExportError, match="verify-and-build"):
        _export(source, tmp_path / "target2")

    _write(source / "packaging/private/ci.yml", SCALAR_STEPS_YAML)
    with pytest.raises(PrivateExportError, match="steps"):
        _export(source, tmp_path / "target3")


def test_failed_validation_publishes_nothing_and_keeps_old_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"

    _export(source, target)
    before = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }

    _write(source / "packaging/private/ci.yml", BROKEN_HERE_STRING_YAML)
    with pytest.raises(PrivateExportError):
        _export(source, target)

    after = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }
    assert after == before

    # 全新目标同样必须保持未发布：不产生目标目录或残留staging。
    fresh_target = tmp_path / "fresh-target"
    with pytest.raises(PrivateExportError, match="YAML"):
        _export(source, fresh_target)
    assert not fresh_target.exists()
    leftovers = [
        entry.name
        for entry in tmp_path.iterdir()
        if entry.name.startswith(".private-export-")
    ]
    assert leftovers == []


def test_exported_ci_workflow_reparses_with_base_loader(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"

    _export(source, target)

    document = yaml.load(
        (target / WORKFLOW_TARGET).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(document, dict)
    triggers = document.get("on")
    assert isinstance(triggers, dict)
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers


def test_exported_private_tree_passes_ci_contract_tests(tmp_path: Path) -> None:
    """从主线真实源码创建完整私有导出，并在导出树内直接运行私有CI契约测试。

    这是“导出后直接运行私有测试”门禁：导出树必须能够独立通过
    tests/test_ci_workflow.py。根.gitattributes、.github/workflows/ci.yml、
    tests/test_ci_workflow.py 都由导出白名单/映射真实提供，不得在导出后伪造。
    """
    repo_root = Path(__file__).resolve().parents[1]
    target = tmp_path / "private-export"
    _export(repo_root, target)

    for required in (
        GITATTRIBUTES_TARGET,
        WORKFLOW_TARGET,
        "tests/test_ci_workflow.py",
    ):
        assert (target / required).is_file(), required
        assert Path(repo_root / "packaging/private/test_ci_workflow.py").is_file()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ci_workflow.py", "-q"],
        cwd=target,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "导出后的私有目录形态无法独立通过CI契约测试："
        "tests/test_ci_workflow.py\n"
        f"退出码：{result.returncode}\n"
        "--- stdout ---\n"
        f"{result.stdout}\n"
        "--- stderr ---\n"
        f"{result.stderr}\n"
    )


# ---- 私有Git检出行尾确定性契约（Run 32582853991根因修复）----


def test_private_mapping_includes_gitattributes() -> None:
    """规范源packaging/private/gitattributes必须精确映射为根.gitattributes。"""
    assert PRIVATE_MAPPINGS.get(GITATTRIBUTES_SOURCE) == GITATTRIBUTES_TARGET


def test_gitattributes_canonical_source_is_exact_bytes() -> None:
    spec = Path(__file__).resolve().parents[1] / GITATTRIBUTES_SOURCE
    assert spec.read_bytes() == EXACT_GITATTRIBUTES


def test_export_contains_root_gitattributes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"

    manifest = _export(source, target)

    paths = {item["path"] for item in manifest["files"]}
    assert GITATTRIBUTES_TARGET in paths
    assert (target / GITATTRIBUTES_TARGET).read_bytes() == EXACT_GITATTRIBUTES


def test_manifest_records_exact_gitattributes_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"

    manifest = _export(source, target)

    entries = [
        item
        for item in manifest["files"]
        if item["path"] == GITATTRIBUTES_TARGET
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["bytes"] == len(EXACT_GITATTRIBUTES)
    assert entry["sha256"] == hashlib.sha256(EXACT_GITATTRIBUTES).hexdigest()


def test_tampered_or_missing_gitattributes_fails_verification(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target = tmp_path / "target"
    _export(source, target)

    (target / GITATTRIBUTES_TARGET).write_bytes(b"* text=auto\n")
    errors = verify(target)
    assert any(GITATTRIBUTES_TARGET in error for error in errors)

    (target / GITATTRIBUTES_TARGET).unlink()
    errors = verify(target)
    assert any(GITATTRIBUTES_TARGET in error for error in errors)


def test_repeated_exports_keep_gitattributes_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _make_source(source)
    target1 = tmp_path / "target1"
    target2 = tmp_path / "target2"

    _export(source, target1)
    _export(source, target2)

    assert (target1 / GITATTRIBUTES_TARGET).read_bytes() == EXACT_GITATTRIBUTES
    assert (
        (target1 / GITATTRIBUTES_TARGET).read_bytes()
        == (target2 / GITATTRIBUTES_TARGET).read_bytes()
    )


def _run_git(git: str, *args: str, cwd: Path) -> None:
    subprocess.run(
        [git, *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_real_git_checkout_autocrlf_true_preserves_all_manifest_bytes(
    tmp_path: Path,
) -> None:
    """真实Git集成测试：core.autocrlf=true检出后清单内每个文件逐字节不变。

    依赖私有导出根.gitattributes的`* text=auto eol=lf`在checkout时生效；
    本机缺少git时明确失败，不得skip；不得mock、不得事后批量替换换行。
    """
    git = shutil.which("git")
    assert git is not None, "本机必须安装git才能验证行尾确定性；不得skip"

    source = tmp_path / "source"
    _make_source(source)
    export_dir = tmp_path / "export"
    _export(source, export_dir)

    seed = tmp_path / "seed"
    shutil.copytree(export_dir, seed)
    _run_git(git, "init", "-q", ".", cwd=seed)
    # seed侧固定autocrlf=false：入库对象保持原始LF字节。
    _run_git(git, "-c", "core.autocrlf=false", "add", "-A", cwd=seed)
    _run_git(
        git,
        "-c",
        "user.name=export-test",
        "-c",
        "user.email=export-test@example.invalid",
        "commit",
        "-qm",
        "seed private export",
        cwd=seed,
    )

    checkout_dir = tmp_path / "checkout"
    # 本机git的clone -c不会把配置写入新仓库本地config；
    # 用no-checkout + 显式local config + 首次checkout，
    # 精确实现“以core.autocrlf=true检出到全新目录”。
    subprocess.run(
        [git, "clone", "-q", "--no-checkout", str(seed), str(checkout_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(git, "config", "--local", "core.autocrlf", "true", cwd=checkout_dir)
    _run_git(git, "checkout", "HEAD", "--", ".", cwd=checkout_dir)
    autocrlf = subprocess.run(
        [git, "config", "--local", "core.autocrlf"],
        cwd=checkout_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert autocrlf == "true"

    manifest = json.loads(
        (export_dir / "shared-source-manifest.json").read_text(encoding="utf-8")
    )
    changed = []
    for entry in manifest["files"]:
        rel = entry["path"]
        if (export_dir / rel).read_bytes() != (checkout_dir / rel).read_bytes():
            changed.append(rel)
    assert changed == [], f"{len(changed)}个文件在autocrlf=true检出后改变：{changed[:10]}"

    assert verify(checkout_dir) == []
