"""Chrome 导入在真实 Windows 占用场景下的错误边界。"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from weibo_book.chrome_import import import_from_chrome
from weibo_book.errors import WeiboError


def test_permission_error_from_locked_profile_becomes_weibo_error(tmp_path):
    """隔离浏览器目录被占用（PermissionError）必须转成中文 WeiboError。

    真实 Windows 根因：上一次隔离实例未退净或杀软扫描持有
    chrome-import-profile 目录句柄时，launch_persistent_context 抛出
    PermissionError；生产函数原先没有异常边界，原始异常直穿调用方。
    """
    class FakeBrowser:
        def new_page(self):  # pragma: no cover - 不会到达
            raise AssertionError("不应创建页面")

        def close(self):  # pragma: no cover - 不会到达
            pass

    def locked_profile(*_args, **_kwargs):
        raise PermissionError(13, "另一个程序正在使用此文件，进程无法访问。")

    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=locked_profile)
    )

    @contextmanager
    def fake_sync_playwright():
        yield fake_playwright

    with patch("weibo_book.chrome_import._find_chrome", return_value="chrome.exe"), \
         patch("weibo_book.chrome_import._is_chrome_running", return_value=False), \
         patch("weibo_book.chrome_import._chrome_profile_dir", return_value=tmp_path / "profile"), \
         patch("playwright.sync_api.sync_playwright", fake_sync_playwright):
        with pytest.raises(WeiboError) as excinfo:
            import_from_chrome()

    assert "Chrome" in excinfo.value.args[0]
    assert excinfo.value.__cause__ is not None
