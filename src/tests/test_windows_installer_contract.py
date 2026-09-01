"""Windows 安装与卸载契约静态测试。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer.iss"


def _text() -> str:
    return INSTALLER.read_text(encoding="utf-8-sig")


def test_installer_is_utf8_bom() -> None:
    data = INSTALLER.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), "installer.iss 必须保存为 UTF-8 BOM"


def test_installer_defines_single_get_user_data_root() -> None:
    text = _text()
    assert "function GetUserDataRoot(): String;" in text
    assert text.count("function GetUserDataRoot(): String;") == 1


def test_get_user_data_root_uses_localappdata_env_with_fallback() -> None:
    text = _text()
    assert (
        "{%LOCALAPPDATA|{localappdata}}\\{#MyAppNameEn}" in text
    )
    # 环境变量 LOCALAPPDATA 存在时与应用运行时同源；缺失时回退 {localappdata}。
    assert "ExpandConstant(" in text
    assert "LOCALAPPDATA" in text
    assert "{localappdata}" in text


def test_installer_defines_keep_and_delete_userdata_params() -> None:
    text = _text()
    assert "KeepUserData" in text
    assert "DeleteUserData" in text
    assert "/KEEPUSERDATA=1" in text
    assert "/DELETEUSERDATA=1" in text


def test_installer_rejects_both_params() -> None:
    text = _text()
    assert "不能同时使用" in text
    assert "IsKeepUserDataParam() and IsDeleteUserDataParam()" in text


def test_installer_does_not_delete_global_playwright_cache() -> None:
    text = _text()
    # 脚本可以提到 ms-playwright 作为解释，但不能在卸载清理分支里删除全局缓存；
    # 全脚本只允许一条 DelTree，且目标必须是 GetUserDataRoot()。
    assert "DelTree(ExpandConstant('{localappdata}\\ms-playwright')" not in text
    assert text.count("DelTree(") == 1
    assert "DelTree(GetUserDataRoot(), True, True, True);" in text


def test_installer_has_no_unconditional_uninstalldelete_section() -> None:
    text = _text()
    assert "[UninstallDelete]" not in text
    for name in ("{localappdata}\\{#MyAppNameEn}\\cache", "{localappdata}\\{#MyAppNameEn}\\logs", "{localappdata}\\{#MyAppNameEn}\\state"):
        assert name not in text


def test_keep_userdata_path_never_calls_deltree() -> None:
    text = _text()
    keep_start = text.find("if IsKeepUserDataParam() then")
    assert keep_start >= 0
    # KEEP 分支从 Begin 到 next Exit 没有清理调用。
    segment = text[keep_start : keep_start + 300]
    assert "DelTree" not in segment


def test_delete_userdata_path_calls_deltree_on_get_user_data_root() -> None:
    text = _text()
    assert "DelTree(GetUserDataRoot(), True, True, True);" in text
    assert "DelTree(ExpandConstant('{localappdata}\\{#MyAppNameEn}')" not in text


def test_uninstall_prompt_uses_get_user_data_root() -> None:
    text = _text()
    prompt_start = text.find("是否同时删除以下内容")
    assert prompt_start >= 0
    prompt = text[prompt_start : prompt_start + 300]
    assert "GetUserDataRoot()" in prompt


def test_uninstall_deletes_primary_cookie_file() -> None:
    text = _text()
    # 登录凭证在用户主目录而非 LOCALAPPDATA，卸载清理分支必须单独删除；
    # 用户自选的微博书档案目录卸载不触碰。
    assert "DeleteFile(ExpandConstant('{%USERPROFILE}\\.weibo_book_cookies'));" in text
