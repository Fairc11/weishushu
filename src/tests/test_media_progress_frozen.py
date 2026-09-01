"""frozen Windows console=False 时 tqdm 进度条兜底契约。

原方案 2026-07-25 tqdm 崩溃修复测试：
当 sys.stderr 和 sys.stdout 都已关闭 / 为 None 时，_media_progress 返回 disabled tqdm
且 pbar.update(1) 不抛异常。

v1.3 阶段 1 增强：frozen=True 时，即使 sys.stderr 是"假流"（非 None、closed=False、
write("") 不抛异常），_media_progress 也必须返回 disabled tqdm。日志2 反弹到 media.py:52
的根因是 frozen Windows console=False 下存在"假流"，tqdm status_printer 内部
fp.write('\\r...') 或 fp.flush() 仍会抛 ValueError: I/O operation on closed file。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from weibo_book.media import _media_progress


class _ClosedStream:
    """模拟已关闭的流：closed=True 且 write 抛 ValueError。"""

    closed = True

    def write(self, _data):
        raise ValueError("I/O operation on closed file")

    def flush(self):
        raise ValueError("I/O operation on closed file")


class _FakeStream:
    """模拟 frozen Windows console=False 下的"假流"。

    非 None、closed=False、write("") 不抛异常（空字符串可能 short-circuit），
    但 tqdm status_printer 内部 fp.write('\\r...') 或 fp.flush() 仍会抛
    ValueError: I/O operation on closed file。
    """

    closed = False

    def write(self, data):
        # 空字符串不抛异常，模拟"假流"通过 _writable_console_stream 检测
        if data == "":
            return 0
        # 非空字符串抛异常，模拟 tqdm status_printer 内部崩溃
        raise ValueError("I/O operation on closed file")

    def flush(self):
        raise ValueError("I/O operation on closed file")


class MediaProgressFrozenConsoleTests(unittest.TestCase):
    """_media_progress 在 frozen 无控制台场景下的行为。"""

    def test_returns_disabled_tqdm_when_stderr_and_stdout_are_none(self):
        with patch("weibo_book.media.sys.stderr", None), patch(
            "weibo_book.media.sys.stdout", None
        ):
            pbar = _media_progress(10)
        try:
            self.assertTrue(pbar.disable)
            pbar.update(1)
        finally:
            pbar.close()

    def test_returns_disabled_tqdm_when_streams_closed(self):
        closed = _ClosedStream()
        with patch("weibo_book.media.sys.stderr", closed), patch(
            "weibo_book.media.sys.stdout", closed
        ):
            pbar = _media_progress(5)
        try:
            self.assertTrue(pbar.disable)
            pbar.update(1)
        finally:
            pbar.close()

    def test_returns_disabled_tqdm_when_frozen_even_with_fake_stream(self):
        """frozen=True 时，即使 sys.stderr 是"假流"，也必须返回 disabled tqdm。

        复现日志2崩溃路径：frozen Windows console=False 下 _writable_console_stream
        误判"假流"可用，tqdm status_printer 内部崩溃。
        """
        fake = _FakeStream()
        with patch("weibo_book.media.sys.frozen", True, create=True), patch(
            "weibo_book.media.sys.stderr", fake
        ), patch("weibo_book.media.sys.stdout", fake):
            pbar = _media_progress(8)
        try:
            self.assertTrue(pbar.disable)
            pbar.update(1)
        finally:
            pbar.close()

    def test_returns_disabled_tqdm_when_frozen_even_with_real_stream(self):
        """frozen=True 时，即使有真实可写流，也必须返回 disabled tqdm。

        frozen 模式没有真实控制台，tqdm 进度条用户看不到，禁用不影响任何可见功能。
        """
        import io

        real_stream = io.StringIO()
        with patch("weibo_book.media.sys.frozen", True, create=True), patch(
            "weibo_book.media.sys.stderr", real_stream
        ):
            pbar = _media_progress(3)
        try:
            self.assertTrue(pbar.disable)
            pbar.update(1)
        finally:
            pbar.close()


if __name__ == "__main__":
    unittest.main()
