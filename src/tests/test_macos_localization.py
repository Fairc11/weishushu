"""macOS 目录选择器与应用本地化契约。"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import js_api
from js_api import JsApi


ROOT = Path(__file__).resolve().parents[1]


def test_non_macos_folder_dialog_uses_current_file_dialog_enum(tmp_path):
    api = JsApi()
    window = MagicMock()
    window.create_file_dialog.return_value = (str(tmp_path),)
    api.set_window(window)

    with patch.object(js_api.sys, "platform", "linux"):
        assert api.select_folder(str(tmp_path)) == str(tmp_path)

    import webview
    window.create_file_dialog.assert_called_once_with(webview.FileDialog.FOLDER, directory=str(tmp_path))


def test_mac_folder_dialog_uses_native_chinese_panel(tmp_path):
    api = JsApi()
    api.set_window(MagicMock())

    with patch.object(js_api.sys, "platform", "darwin"), patch.object(
        api, "_select_folder_macos", return_value=str(tmp_path)
    ) as native:
        assert api.select_folder(str(tmp_path)) == str(tmp_path)

    native.assert_called_once_with(str(tmp_path))


def test_mac_bundle_declares_simplified_chinese_localization():
    spec = (ROOT / "build_mac.spec").read_text(encoding="utf-8")

    assert '"CFBundleDevelopmentRegion": "zh-Hans"' in spec
    assert '"CFBundleLocalizations": ["zh-Hans"]' in spec


def test_desktop_window_passes_chinese_pywebview_localization():
    source = (ROOT / "desktop_app.py").read_text(encoding="utf-8")

    assert '"global.cancel": "取消"' in source
    assert "localization=CHINESE_LOCALIZATION" in source
