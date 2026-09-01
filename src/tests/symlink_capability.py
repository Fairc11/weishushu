"""真实符号链接能力探测。

在创建真实符号链接的用例前调用 `require_symlink_capability`：
- 实际探测当前账户能否创建文件/目录符号链接（结果按类型缓存）；
- 仅当精确捕获 WinError 1314（无 SeCreateSymbolicLinkPrivilege）时允许跳过；
- 其他 OSError 原样抛出，不得扩大跳过范围。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

WINERROR_PRIVILEGE_NOT_HELD = 1314

_capability_cache: dict[bool, bool] = {}


def _probe_once(target_is_directory: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="weishushu-symlink-probe-") as td:
        root = Path(td)
        target = root / "target"
        link = root / ("link-dir" if target_is_directory else "link-file")
        if target_is_directory:
            target.mkdir()
        else:
            target.write_text("probe", encoding="utf-8")
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) == WINERROR_PRIVILEGE_NOT_HELD:
                return False
            raise
    return True


def require_symlink_capability(*, target_is_directory: bool = False) -> None:
    """无对应符号链接能力时跳过；探测遇到其他 OSError 时直接失败。"""
    if target_is_directory not in _capability_cache:
        _capability_cache[target_is_directory] = _probe_once(target_is_directory)
    if not _capability_cache[target_is_directory]:
        pytest.skip("当前账户无符号链接创建权限（WinError 1314）")


def reset_symlink_capability_cache() -> None:
    """仅供测试复位探测缓存。"""
    _capability_cache.clear()
