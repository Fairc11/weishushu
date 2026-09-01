"""Mac 环境与完整锁一致性门禁测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import scripts.verify_macos_lock as verify

ROOT = Path(__file__).resolve().parents[1]


def test_build_mac_runs_lock_verify_before_playwright_and_pyinstaller() -> None:
    source = (ROOT / "scripts" / "build_mac.sh").read_text(encoding="utf-8")
    lock_pos = source.find("scripts/verify_macos_lock.py")
    playwright_pos = source.find("playwright install chromium")
    pyinstaller_pos = source.find("PyInstaller --version")
    assert lock_pos >= 0
    assert playwright_pos > lock_pos
    assert pyinstaller_pos > lock_pos


def test_verify_macos_lock_passes_when_env_matches_lock(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text("a==1\nb==2\n", encoding="utf-8")
    monkeypatch.setattr(verify, "LOCK", lock, raising=False)
    monkeypatch.setattr(verify, "_freeze_set", lambda: {"a==1", "b==2"}, raising=False)
    assert verify.main() == 0


def test_verify_macos_lock_fails_when_env_has_extra_or_missing(tmp_path, monkeypatch) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text("a==1\nb==2\n", encoding="utf-8")
    monkeypatch.setattr(verify, "LOCK", lock, raising=False)
    monkeypatch.setattr(verify, "_freeze_set", lambda: {"a==1", "c==3"}, raising=False)
    assert verify.main() == 1
