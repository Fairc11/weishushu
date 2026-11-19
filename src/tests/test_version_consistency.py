from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = "2.2.0"


def test_runtime_version_sources_match_current():
    from backend.app.config import Settings
    from backend.app.version import VERSION
    from js_api import JsApi
    from weibo_book import __version__

    assert VERSION == CURRENT_VERSION
    assert Settings().version == CURRENT_VERSION
    assert __version__ == CURRENT_VERSION
    assert JsApi().get_version() == CURRENT_VERSION


def test_packaging_version_sources_match_current():
    mac_spec = (ROOT / "build_mac.spec").read_text(encoding="utf-8")
    installer = (ROOT / "installer.iss").read_text(encoding="utf-8-sig")
    build_bat = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
    version_reader = (ROOT / "scripts" / "read_version.py").read_text(
        encoding="utf-8"
    )

    assert '"CFBundleShortVersionString": "2.2.0"' in mac_spec
    assert '"CFBundleVersion": "2.2.0"' in mac_spec
    assert '#define MyAppVersion "2.2.0"' in installer
    assert 'set "MY_VER=2.2.0"' in build_bat
    assert 'm else "2.2.0"' in version_reader


def test_current_first_run_and_user_visible_versions_match_current():
    from frontend_assets import frontend_bundle_asset
    from backend.app.services.first_run import ACCEPTED_VERSION, MARKER_FILENAME

    base = (ROOT / "backend" / "app" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    app_js = frontend_bundle_asset().read_text(encoding="utf-8")

    assert ACCEPTED_VERSION == CURRENT_VERSION
    assert MARKER_FILENAME == "first_run_v2.2.0.json"
    assert "微书薯 v2.2.0 风险须知" in base
    assert "已接受 v2.2.0 风险须知" in app_js
