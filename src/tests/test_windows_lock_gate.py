"""Windows x64 完整依赖锁门禁测试。"""

from __future__ import annotations

import re
from pathlib import Path

import scripts.verify_windows_lock as verify

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "requirements" / "lock-windows-x64.txt"


def _lock_lines() -> list[str]:
    return verify.parse_lock_requirements(LOCK_PATH.read_text(encoding="utf-8"))


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_windows_lock_exists_and_is_not_placeholder() -> None:
    assert LOCK_PATH.is_file()
    text = LOCK_PATH.read_text(encoding="utf-8")
    for marker in verify.PLACEHOLDER_MARKERS:
        assert marker not in text, marker


def test_windows_lock_rejects_unsafe_requirement_lines() -> None:
    unsafe = [
        "-e editable==1.0",
        "--editable other==2.0",
        "pkg @ file:///tmp/pkg-1.0.whl",
        "pkg @ https://example.com/pkg-1.0.whl",
        "pkg==1.0  # needs >=2",
        "pkg>=1.0",
        "pkg",
        "pkg==1.0; python_version>='3.12'",
        "pkg @ https://user:token@example.com/pkg.whl",
        r"C:\pkgs\pkg==1.0",
        "pkg==1.0 --index-url https://user:pass@evil.example/simple",
    ]
    for line in unsafe:
        errors = verify.audit_lock_lines([line])
        assert errors, f"应拒绝: {line}"


def test_windows_lock_accepts_pinned_plain_lines() -> None:
    assert verify.audit_lock_lines(["altgraph==0.17.5", "pywebview==6.2.1"]) == []


def test_real_windows_lock_passes_audit() -> None:
    errors = verify.audit_lock_lines(_lock_lines())
    assert errors == []


def test_windows_lock_covers_all_direct_dependencies() -> None:
    lock_names = {
        _normalized(line.split("==", 1)[0]) for line in _lock_lines()
    }
    direct: set[str] = set()
    for req_file in (
        ROOT / "requirements" / "common.txt",
        ROOT / "requirements" / "windows.txt",
    ):
        for raw in req_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-r ", "--requirement ")):
                continue
            direct.add(_normalized(re.split(r"[<>=!~\[ ;@]", line, 1)[0]))
    missing = sorted(direct - lock_names)
    assert not missing, f"完整锁缺少直接依赖: {missing}"


def test_windows_lock_pins_desktop_stack_exactly() -> None:
    pinned = {line.split("==", 1)[0]: line.split("==", 1)[1] for line in _lock_lines()}
    expected = {
        "pywebview": "6.2.1",
        "pythonnet": "3.1.0",
        "clr_loader": "0.3.1",
        "pywin32": "311",
        "comtypes": "1.4.16",
    }
    for name, version in expected.items():
        assert pinned.get(name) == version, f"{name} 必须精确固定为 {version}"


def test_windows_requirements_do_not_use_invalid_winforms_extra() -> None:
    text = (ROOT / "requirements" / "windows.txt").read_text(encoding="utf-8")
    assert "[winforms]" not in text
    assert "pywebview==6.2.1" in text


def test_verify_windows_lock_main_passes_on_real_lock(monkeypatch) -> None:
    monkeypatch.setattr(verify, "LOCK", LOCK_PATH, raising=False)
    assert verify.main() == 0
