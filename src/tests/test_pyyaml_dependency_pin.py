"""PyYAML直接依赖声明锁定：common.txt与两套完整锁必须精确为6.0.3。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "PyYAML==6.0.3"


def _pinned_lines(relative: str) -> set[str]:
    text = (ROOT / relative).read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_common_txt_declares_pyyaml_direct_dependency() -> None:
    assert EXPECTED in _pinned_lines("requirements/common.txt")


def test_complete_locks_keep_pyyaml_exact_version() -> None:
    for lock in ("requirements/lock-windows-x64.txt", "requirements/lock-macos-arm64.txt"):
        pyyaml_lines = {
            line for line in _pinned_lines(lock) if line.startswith("PyYAML")
        }
        assert pyyaml_lines == {EXPECTED}, (lock, pyyaml_lines)
