"""封包两套 spec 的静态资源契约测试。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_both_specs_include_self_test_fixtures() -> None:
    windows_spec = (ROOT / "build.spec").read_text(encoding="utf-8")
    mac_spec = (ROOT / "build_mac.spec").read_text(encoding="utf-8")
    assert 'desktop/self_test/fixtures' in windows_spec
    assert 'desktop/self_test/fixtures' in mac_spec


def test_specs_do_not_include_real_sensitive_data() -> None:
    for name in ("build.spec", "build_mac.spec"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in (
            ".weibo_book_cookies",
            ".weibo_book_cookies_dev",
            "个人数据与开发资料",
            "archive.db",
            "active-personal-archive-task.json",
        ):
            assert forbidden not in text, f"{name} contains {forbidden}"
        # 确保 datas 中没有真实日志/数据库文件作为打包路径。
        for data_line in text.splitlines():
            if data_line.lstrip().startswith("(") and (
                ".log" in data_line or ".db" in data_line or "cookies.json" in data_line
            ):
                raise AssertionError(f"{name} datas contains real data path: {data_line}")
