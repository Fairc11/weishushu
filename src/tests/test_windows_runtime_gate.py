"""Windows 运行时身份门禁测试：在macOS上以模拟Windows事实驱动纯校验逻辑。"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import scripts.verify_windows_runtime as gate
from scripts.verify_windows_runtime import RuntimeFacts, check_runtime_identity


def _windows_facts(**overrides: object) -> RuntimeFacts:
    values: dict[str, object] = {
        "python_version": (3, 12, 13),
        "machine": "AMD64",
        "architecture": "64bit",
        "pip_version": "26.2.1",
        "virtual_env": "C:\\runner-temp\\weishushu-w1-venv",
        "executable": "C:\\runner-temp\\weishushu-w1-venv\\Scripts\\python.exe",
    }
    values.update(overrides)
    return RuntimeFacts(**values)  # type: ignore[arg-type]


def test_consistent_windows_facts_pass() -> None:
    assert check_runtime_identity(_windows_facts()) == []


def test_python_patch_version_mismatch_rejected() -> None:
    errors = check_runtime_identity(_windows_facts(python_version=(3, 12, 14)))
    assert len(errors) == 1
    assert "Python版本" in errors[0]
    assert "3.12.14" in errors[0]


def test_machine_mismatch_rejected() -> None:
    errors = check_runtime_identity(_windows_facts(machine="ARM64"))
    assert len(errors) == 1
    assert "CPU架构" in errors[0]
    assert "ARM64" in errors[0]


def test_architecture_bits_mismatch_rejected() -> None:
    errors = check_runtime_identity(_windows_facts(architecture="32bit"))
    assert len(errors) == 1
    assert "位数" in errors[0]
    assert "32bit" in errors[0]


def test_pip_version_mismatch_rejected() -> None:
    errors = check_runtime_identity(_windows_facts(pip_version="25.0"))
    assert len(errors) == 1
    assert "pip版本" in errors[0]
    assert "25.0" in errors[0]


def test_missing_virtual_env_rejected() -> None:
    empty = check_runtime_identity(_windows_facts(virtual_env=""))
    unset = check_runtime_identity(_windows_facts(virtual_env=None))
    assert len(empty) == 1
    assert len(unset) == 1
    assert all("VIRTUAL_ENV" in error for error in empty + unset)


def test_executable_outside_venv_scripts_rejected() -> None:
    errors = check_runtime_identity(
        _windows_facts(executable="C:\\other-tools\\python.exe")
    )
    assert len(errors) == 1
    assert "Scripts" in errors[0]
    assert "C:\\other-tools\\python.exe" in errors[0]


def test_executable_in_nested_scripts_subdir_rejected() -> None:
    """解释器必须直接位于venv的Scripts目录；更深层路径一律拒绝。"""
    errors = check_runtime_identity(
        _windows_facts(
            virtual_env="C:\\venv",
            executable="C:\\venv\\Scripts\\nested\\python.exe",
        )
    )
    assert len(errors) == 1
    assert "Scripts" in errors[0]
    assert "C:\\venv\\Scripts\\nested\\python.exe" in errors[0]


def test_venv_path_comparison_is_case_insensitive() -> None:
    """Windows路径大小写不敏感；大小写差异不得造成误报。"""
    facts = _windows_facts(
        virtual_env="C:\\Runner-Temp\\WEISHUSHU-W1-VENV",
    )
    assert check_runtime_identity(facts) == []


def test_main_returns_zero_when_facts_consistent(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(gate, "collect_facts", lambda: _windows_facts())
    assert gate.main() == 0
    assert "通过" in capsys.readouterr().out


def test_main_returns_nonzero_and_reports_chinese_errors(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        gate, "collect_facts", lambda: _windows_facts(pip_version="25.0")
    )
    assert gate.main() == 1
    captured = capsys.readouterr()
    assert "pip版本" in captured.err + captured.out


# ---- Windows runner标准流编码契约（Run 32579818846根因防御）----


class _NoReconfigureStream:
    """不支持reconfigure的流（如StringIO或自定义对象）。"""

    def __init__(self) -> None:
        self.written = []

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)


class _ExplodingReconfigureStream:
    """reconfigure抛OSError的流；配置失败不得影响身份校验结果。"""

    def reconfigure(self, **_kwargs: object) -> None:
        raise OSError("stream refused reconfigure")


class _StrictCp1252LikeStream:
    """模拟runner严格cp1252流：write中文主动抛UnicodeEncodeError。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as exc:
            raise UnicodeEncodeError(
                exc.encoding, exc.object, exc.start, exc.end, exc.reason
            ) from None
        self.chunks.append(text)
        return len(text)


def _strict_stream_without_reconfigure() -> _StrictCp1252LikeStream:
    return _StrictCp1252LikeStream()


def _strict_stream_with_failing_reconfigure() -> _StrictCp1252LikeStream:
    stream = _StrictCp1252LikeStream()
    stream.reconfigure = (  # type: ignore[method-assign]
        lambda **_kwargs: (_ for _ in ()).throw(OSError("refused"))
    )
    return stream


def _cp1252_wrapper() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_configure_stdio_reencodes_cp1252_to_utf8() -> None:
    stdout = _cp1252_wrapper()
    stderr = _cp1252_wrapper()
    gate.configure_stdio([stdout, stderr])
    assert stdout.encoding.lower().replace("-", "") == "utf8"
    assert stderr.encoding.lower().replace("-", "") == "utf8"
    assert stdout.errors == "backslashreplace"
    assert stderr.errors == "backslashreplace"


def test_configured_stream_accepts_chinese_success_message() -> None:
    wrapper = _cp1252_wrapper()
    gate.configure_stdio([wrapper])
    wrapper.write("Windows运行时身份门禁通过：Python 3.12.13")
    wrapper.flush()
    payload = wrapper.buffer.getvalue()
    assert "运行时身份门禁".encode("utf-8") in payload


def test_configure_stdio_tolerates_none_streams() -> None:
    gate.configure_stdio([None, None])


def test_configure_stdio_tolerates_streams_without_reconfigure() -> None:
    stream = _NoReconfigureStream()
    gate.configure_stdio([stream])
    stream.write("中文")
    assert stream.written == ["中文"]


def test_configure_stdio_failure_does_not_mask_identity_result(
    monkeypatch,
) -> None:
    """closed/异常流下配置必须静默让路，main()照常返回身份校验结论。"""
    closed = _cp1252_wrapper()
    closed.close()
    exploding = _ExplodingReconfigureStream()
    monkeypatch.setattr(sys, "stdout", closed, raising=False)
    monkeypatch.setattr(sys, "stderr", exploding, raising=False)
    monkeypatch.setattr(gate, "collect_facts", lambda: _windows_facts())
    assert gate.main() == 0


def _make_cp1252_failure_env(base_env: dict[str, str]) -> dict[str, str]:
    """构造cp1252失败身份路径的子进程环境。

    显式删除PYTHONUTF8与VIRTUAL_ENV，使子进程无论父进程在Mac/Windows
    是否已有合法虚拟环境，都稳定走到“环境变量VIRTUAL_ENV未设置或为空”失败路径。
    """
    env = dict(base_env)
    env["PYTHONIOENCODING"] = "cp1252"
    env.pop("PYTHONUTF8", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def test_cp1252_failure_env_drops_inherited_virtual_env() -> None:
    """父环境即使设置合法VIRTUAL_ENV，失败路径env也必须删除它。"""
    parent = dict(os.environ)
    parent["VIRTUAL_ENV"] = "C:\\fake-parent\\venv"
    env = _make_cp1252_failure_env(parent)
    assert env.get("PYTHONIOENCODING") == "cp1252"
    assert "PYTHONUTF8" not in env
    assert "VIRTUAL_ENV" not in env


def test_subprocess_cp1252_environment_reports_chinese_without_crash() -> None:
    """PYTHONIOENCODING=cp1252下失败身份路径：非零退出、无UnicodeEncodeError、
    输出可按UTF-8解码且保留明确的VIRTUAL_ENV缺失错误。

    Run 32619501174根因：测试继承W1合法VIRTUAL_ENV，使身份门禁正确返回0。
    本测试显式删除VIRTUAL_ENV，使失败路径不再依赖主机或CI运行器环境。
    """
    parent = dict(os.environ)
    parent["VIRTUAL_ENV"] = "C:\\fake-parent\\venv"
    env = _make_cp1252_failure_env(parent)
    script = Path(gate.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert b"UnicodeEncodeError" not in combined
    decoded = combined.decode("utf-8")  # 不可解码即测试失败
    assert "环境变量VIRTUAL_ENV未设置或为空" in decoded


# ---- 严格流write抛UnicodeEncodeError时的最后防线（总控补全缺口）----


def test_safe_print_swallows_unicode_encode_error_from_strict_write() -> None:
    """_safe_print不得向外抛UnicodeEncodeError（两种严格流形态）。"""
    for stream in (
        _strict_stream_without_reconfigure(),
        _strict_stream_with_failing_reconfigure(),
    ):
        gate._safe_print("Windows运行时身份门禁通过：中文", stream)
        assert stream.chunks == []


def test_strict_stdout_failure_keeps_success_identity_exit_zero(
    monkeypatch,
) -> None:
    strict = _StrictCp1252LikeStream()
    monkeypatch.setattr(sys, "stdout", strict)
    monkeypatch.setattr(sys, "stderr", _StrictCp1252LikeStream())
    monkeypatch.setattr(gate, "collect_facts", lambda: _windows_facts())
    assert gate.main() == 0
    assert strict.chunks == []


def test_strict_stderr_failure_does_not_mask_identity_failure(
    monkeypatch,
) -> None:
    """输出失败不得把身份失败改成成功：main仍返回1。"""
    strict = _StrictCp1252LikeStream()
    monkeypatch.setattr(sys, "stdout", _StrictCp1252LikeStream())
    monkeypatch.setattr(sys, "stderr", strict)
    monkeypatch.setattr(
        gate, "collect_facts", lambda: _windows_facts(pip_version="25.0")
    )
    assert gate.main() == 1
    assert strict.chunks == []
