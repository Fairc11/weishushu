"""Windows 二进制媒体复制的低级打开契约。

Windows 上必须使用真实 os.O_BINARY 打开并保留完整 PNG 字节
（含 0x1A 之后字节）；只有 POSIX 才允许用测试标志模拟。
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# 仅 POSIX 模拟用：真实 os.O_BINARY 在 POSIX 不存在（getattr 兜底为 0）。
_TEST_BINARY_FLAG = 1 << 28


def _expected_binary_flag(sync_module) -> int:
    if _IS_WINDOWS:
        return os.O_BINARY
    return _TEST_BINARY_FLAG


def _record_binary_opens(monkeypatch, sync_module):
    """记录 os.open 调用；Windows 不替换、不剥离任何标志位。"""
    real_open = sync_module.os.open
    opened: list[tuple[Path, int]] = []

    if _IS_WINDOWS:
        def recording_open(path, flags, *args, **kwargs):
            opened.append((Path(path), flags))
            return real_open(path, flags, *args, **kwargs)
    else:
        monkeypatch.setattr(
            sync_module, "_O_BINARY", _TEST_BINARY_FLAG, raising=False
        )

        def recording_open(path, flags, *args, **kwargs):
            opened.append((Path(path), flags))
            return real_open(path, flags & ~_TEST_BINARY_FLAG, *args, **kwargs)

    monkeypatch.setattr(sync_module.os, "open", recording_open)
    return opened


PNG_PAYLOAD = b"\x89PNG\r\n\x1a\nbinary-payload"


def test_install_proof_reads_full_bytes_after_1a_marker(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    media = tmp_path / "source.png"
    media.write_bytes(PNG_PAYLOAD)
    opened = _record_binary_opens(monkeypatch, sync_module)

    proof = PersonalArchiveSync._install_proof(media)

    assert proof["sha256"] == hashlib.sha256(PNG_PAYLOAD).hexdigest()
    assert proof["size"] == len(PNG_PAYLOAD)
    expected_flags = os.O_RDONLY | sync_module._O_NOFOLLOW | _expected_binary_flag(sync_module)
    assert opened == [(media, expected_flags)]


def test_windows_safe_copy_preserves_full_png_bytes(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    work_root = tmp_path / "work"
    work_root.mkdir()
    source = work_root / "source.png"
    source.write_bytes(PNG_PAYLOAD)
    monkeypatch.setattr(sync_module, "_SUPPORTS_DIRECTORY_FDS", False)
    opened = _record_binary_opens(monkeypatch, sync_module)

    safe = PersonalArchiveSync.__new__(PersonalArchiveSync)._copy_staged_to_safe(
        work_root, source, 0
    )

    # \r\n 与 0x1A 后字节完整保留，文本模式翻译会破坏此断言。
    assert safe.read_bytes() == PNG_PAYLOAD
    assert len(opened) == 2
    binary_flag = _expected_binary_flag(sync_module)
    assert all(flags & binary_flag for _, flags in opened)


def test_windows_install_copy_preserves_full_png_bytes(tmp_path, monkeypatch):
    from weibo_book.archive import sync as sync_module
    from weibo_book.archive.sync import PersonalArchiveSync

    staged = tmp_path / "staged.png"
    target = tmp_path / "target.png"
    staged.write_bytes(PNG_PAYLOAD)
    monkeypatch.setattr(sync_module, "_SUPPORTS_DIRECTORY_FDS", False)
    opened = _record_binary_opens(monkeypatch, sync_module)

    PersonalArchiveSync._install_staged_without_overwrite(
        target, staged, None, target.name
    )

    assert target.read_bytes() == PNG_PAYLOAD
    assert len(opened) == 2
    binary_flag = _expected_binary_flag(sync_module)
    assert all(flags & binary_flag for _, flags in opened)
