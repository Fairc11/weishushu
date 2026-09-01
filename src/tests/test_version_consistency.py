from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = "2.0.1"
# 已公开发布的最新安装包版本（README/DEVELOPMENT 指向的 Release 工件）
RELEASED_VERSION = "2.0.1"


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

    assert '"CFBundleShortVersionString": "2.0.1"' in mac_spec
    assert '"CFBundleVersion": "2.0.1"' in mac_spec
    assert '#define MyAppVersion "2.0.1"' in installer
    assert 'set "MY_VER=2.0.1"' in build_bat
    assert 'm else "2.0.1"' in version_reader


def test_current_first_run_and_user_visible_versions_match_current():
    from frontend_assets import frontend_bundle_asset
    from backend.app.services.first_run import ACCEPTED_VERSION, MARKER_FILENAME

    base = (ROOT / "backend" / "app" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    app_js = frontend_bundle_asset().read_text(encoding="utf-8")

    assert ACCEPTED_VERSION == CURRENT_VERSION
    assert MARKER_FILENAME == "first_run_v2.0.1.json"
    assert "微书薯 v2.0.1 风险须知" in base
    assert "已接受 v2.0.1 风险须知" in app_js


def test_documents_track_current_dev_and_released_versions():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    public_changelog = (ROOT / "public-export" / "CHANGELOG.md").read_text(
        encoding="utf-8"
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    development = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")

    assert "## [2.0.1] - 2026-09-01" in changelog
    assert "## [2.0.1] - 2026-09-01" in public_changelog
    assert "## [2.0.0]" in changelog
    assert f"Weishushu-v{RELEASED_VERSION}-macOS-arm64.dmg.sha256" in readme
    assert f"Weishushu-v{RELEASED_VERSION}-macOS-arm64.dmg.sha256" in development
