"""符号链接能力探测模块测试。"""

from __future__ import annotations

import pytest

from tests.symlink_capability import (
    WINERROR_PRIVILEGE_NOT_HELD,
    require_symlink_capability,
    reset_symlink_capability_cache,
)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    reset_symlink_capability_cache()
    yield
    reset_symlink_capability_cache()


def test_skips_only_on_exact_winerror_1314(monkeypatch) -> None:
    from pathlib import Path

    def raise_1314(self, target, target_is_directory=False):
        error = OSError()
        error.winerror = WINERROR_PRIVILEGE_NOT_HELD
        raise error

    monkeypatch.setattr(Path, "symlink_to", raise_1314)
    with pytest.raises(pytest.skip.Exception):
        require_symlink_capability(target_is_directory=False)


def test_other_oserror_propagates(monkeypatch) -> None:
    from pathlib import Path

    def raise_permission(self, target, target_is_directory=False):
        error = PermissionError()
        error.winerror = 5
        raise error

    monkeypatch.setattr(Path, "symlink_to", raise_permission)
    with pytest.raises(PermissionError):
        require_symlink_capability(target_is_directory=False)


def test_capability_result_is_cached_per_type(monkeypatch) -> None:
    from pathlib import Path

    calls: list[bool] = []

    def real_probe(self, target, target_is_directory=False):
        calls.append(target_is_directory)
        return None

    monkeypatch.setattr(Path, "symlink_to", real_probe)
    require_symlink_capability(target_is_directory=False)
    require_symlink_capability(target_is_directory=False)
    assert calls == [False]
