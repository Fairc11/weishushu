"""外部运行器兜底测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from desktop.self_test.schema import make_result_missing
from scripts.run_packaged_self_test import run


def _script(executable: Path, body: str) -> Path:
    """跨平台子进程 fixture：Python 负载 + 当前解释器启动器。

    Windows 生成 .cmd 启动器，POSIX 生成带 exec 的 sh 启动器；
    返回可被 run() 直接启动的路径。
    """
    payload = executable.parent / f"{executable.name}.payload.py"
    payload.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = executable.with_suffix(".cmd")
        launcher.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{payload}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = executable
        launcher.write_text(
            "#!/bin/sh\n"
            f"exec '{sys.executable}' '{payload}' \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def _json_writer_body(payload: dict, exit_code: int) -> str:
    return (
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "out = Path(args[args.index('--self-test-output') + 1])\n"
        f"out.write_text(json.dumps({payload!r}), encoding='utf-8')\n"
        f"sys.exit({exit_code})\n"
    )


def _exit_zero_body() -> str:
    return "import sys\nsys.exit(0)\n"


def _ok_json(output: Path) -> None:
    result = make_result_missing(build_commit="a", profile="b", platform="c", message="x")
    result["error_kind"] = None
    result["message"] = ""
    result["steps"] = [
        {"name": "manifest_identity", "status": "passed", "message": ""},
        {"name": "json_saved", "status": "passed", "message": ""},
    ]
    output.write_text(json.dumps(result), encoding="utf-8")


def _payload_with_commit(build_commit: str) -> dict:
    return {
        "schema_version": 1,
        "build_commit": build_commit,
        "profile": "user",
        "platform": "darwin",
        "steps": [
            {"name": "manifest_identity", "status": "passed", "message": ""},
            {"name": "json_saved", "status": "passed", "message": ""},
        ],
        "error_kind": None,
        "message": "",
        "log_path": "",
    }


def _write_manifest(path: Path, source_commit: str) -> Path:
    from packaging.build_manifest import make_manifest, write_manifest

    manifest = make_manifest(
        app_version="2.0.0",
        source_commit=source_commit,
        platform="darwin",
        architecture="arm64",
        python_version="3.12.13",
        pyinstaller_version="6.0.0",
        dependency_lock_sha256="d" * 64,
        profile="user",
        executable_name="Weishushu",
        bundle_identifier="com.weishushu.desktop",
        resources=[],
    )
    write_manifest(path, manifest)
    return path


def test_runner_missing_executable_returns_result_missing(tmp_path: Path) -> None:
    result = run(tmp_path / "missing.exe", "packaged-self-test", tmp_path / "out.json")
    assert result["error_kind"] == "result_missing"


def test_runner_creates_fallback_when_process_writes_no_json(tmp_path: Path) -> None:
    executable = _script(tmp_path / "app", _exit_zero_body())
    output = tmp_path / "out.json"
    result = run(executable, "packaged-self-test", output)
    assert result["error_kind"] == "result_missing"
    assert "未写入 JSON" in result["message"]


def test_runner_rejects_existing_output_before_launch(tmp_path: Path) -> None:
    executable = _script(tmp_path / "app", _exit_zero_body())
    output = tmp_path / "out.json"
    _ok_json(output)
    result = run(executable, "packaged-self-test", output)
    assert result["error_kind"] == "result_missing"
    assert "输出文件已存在" in result["message"]


def test_runner_flags_nonzero_exit_even_if_json_says_passed(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "build_commit": "a",
        "profile": "user",
        "platform": "linux",
        "steps": [
            {"name": "json_saved", "status": "passed", "message": ""}
        ],
        "error_kind": None,
        "message": "",
        "log_path": "",
    }
    executable = _script(
        tmp_path / "app",
        _json_writer_body(payload, exit_code=1),
    )
    output = tmp_path / "out.json"
    result = run(executable, "packaged-self-test", output)
    assert result["error_kind"] == "process_failed"
    assert "退出码 1" in result["message"]


def test_runner_flags_failed_steps_even_if_process_exits_zero(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "build_commit": "a",
        "profile": "user",
        "platform": "linux",
        "steps": [
            {"name": "manifest_identity", "status": "failed", "message": "bad"},
            {"name": "json_saved", "status": "passed", "message": ""}
        ],
        "error_kind": None,
        "message": "",
        "log_path": "",
    }
    executable = _script(
        tmp_path / "app",
        _json_writer_body(payload, exit_code=0),
    )
    output = tmp_path / "out.json"
    result = run(executable, "packaged-self-test", output)
    assert result["error_kind"] == "step_failed"
    assert "manifest_identity" in result["message"]


def test_runner_accepts_json_commit_matching_manifest(tmp_path: Path) -> None:
    executable = _script(
        tmp_path / "app",
        _json_writer_body(_payload_with_commit("cafe123"), exit_code=0),
    )
    output = tmp_path / "out.json"
    result = run(
        executable,
        "packaged-self-test",
        output,
        expected_build_commit="cafe123",
    )
    assert result["error_kind"] is None


def test_runner_rejects_json_commit_mismatching_manifest(tmp_path: Path) -> None:
    executable = _script(
        tmp_path / "app",
        _json_writer_body(_payload_with_commit("old123"), exit_code=0),
    )
    output = tmp_path / "out.json"
    result = run(
        executable,
        "packaged-self-test",
        output,
        expected_build_commit="new456",
    )
    assert result["error_kind"] == "process_failed"
    assert "build_commit" in result["message"]
    assert "不一致" in result["message"]


def test_runner_rejects_unknown_json_commit(tmp_path: Path) -> None:
    executable = _script(
        tmp_path / "app",
        _json_writer_body(_payload_with_commit("unknown"), exit_code=0),
    )
    output = tmp_path / "out.json"
    result = run(
        executable,
        "packaged-shell-smoke",
        output,
        expected_build_commit="new456",
    )
    assert result["error_kind"] == "process_failed"
    assert "unknown" in result["message"]


def test_main_reads_expected_commit_from_manifest(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import scripts.run_packaged_self_test as runner

    manifest = _write_manifest(tmp_path / "manifest.json", "cafe123")
    executable = _script(
        tmp_path / "app",
        _json_writer_body(_payload_with_commit("cafe123"), exit_code=0),
    )
    output = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_packaged_self_test.py",
            "--executable",
            str(executable),
            "--mode",
            "packaged-self-test",
            "--self-test-output",
            str(output),
            "--manifest",
            str(manifest),
        ],
    )
    assert runner.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["build_commit"] == "cafe123"


def test_main_fails_when_manifest_missing(tmp_path: Path, monkeypatch) -> None:
    import scripts.run_packaged_self_test as runner

    executable = _script(tmp_path / "app", _exit_zero_body())
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_packaged_self_test.py",
            "--executable",
            str(executable),
            "--mode",
            "packaged-self-test",
            "--self-test-output",
            str(tmp_path / "out.json"),
            "--manifest",
            str(tmp_path / "missing.json"),
        ],
    )
    assert runner.main() == 2


def test_main_returns_3_for_environment_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """桌面壳无桌面会话时运行器以专用退出码3透传，且仍经清单提交校验。"""
    import scripts.run_packaged_self_test as runner
    from desktop.self_test.schema import new_result, set_error

    manifest = _write_manifest(tmp_path / "manifest.json", "cafe123")
    executable = tmp_path / "app"
    _script(executable, _exit_zero_body())
    output = tmp_path / "shell.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_packaged_self_test.py",
            "--executable",
            str(executable),
            "--mode",
            "packaged-shell-smoke",
            "--self-test-output",
            str(output),
            "--manifest",
            str(manifest),
        ],
    )
    unavailable = new_result(
        build_commit="cafe123", profile="user", platform="win32"
    )
    set_error(unavailable, "environment_unavailable", "没有可用桌面会话")
    monkeypatch.setattr(runner, "run", lambda *_a, **_k: unavailable)

    assert runner.main() == 3


def test_run_dispatches_functional_self_test(tmp_path: Path, monkeypatch) -> None:
    import run
    output = tmp_path / "self-test.json"
    monkeypatch.setattr(
        "sys.argv",
        ["run.py", "--packaged-self-test", "--self-test-output", str(output)],
    )
    code = run.main()
    assert code == 1
    assert output.exists()


def test_run_dispatches_shell_smoke_to_environment_unavailable(tmp_path: Path, monkeypatch) -> None:
    import run
    import desktop.self_test.shell as shell_module
    output = tmp_path / "shell.json"
    monkeypatch.setattr(shell_module, "_desktop_session_unavailable", lambda: True)
    monkeypatch.setattr(
        "sys.argv",
        ["run.py", "--packaged-shell-smoke", "--self-test-output", str(output)],
    )
    code = run.main()
    assert code == 3
    assert output.exists()


def test_run_rejects_conflicting_self_test_modes(tmp_path: Path, monkeypatch) -> None:
    import run
    output = tmp_path / "out.json"
    monkeypatch.setattr(
        "sys.argv",
        ["run.py", "--packaged-self-test", "--packaged-shell-smoke", "--self-test-output", str(output)],
    )
    assert run.main() == 1
    assert not output.exists()