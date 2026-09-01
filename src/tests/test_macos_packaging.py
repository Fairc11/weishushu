"""macOS .app 打包配置与 frozen 资源路径契约。"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class MacPackagingTests(unittest.TestCase):
    def _run_profile_probe(
        self,
        script_name: str,
        arguments: list[str],
        *,
        initial_profile: str,
    ) -> str:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            probe_root = Path(td)
            scripts_dir = probe_root / "scripts"
            scripts_dir.mkdir()
            shutil.copy2(root / "scripts" / script_name, scripts_dir / script_name)

            profile_log = probe_root / "profile.log"
            fake_python = probe_root / "fake-python"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s' \"${WEISHUSHU_PROFILE-}\" > \"$PROFILE_LOG\"\n"
                "exit 23\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            fake_bin = probe_root / "fake-bin"
            fake_bin.mkdir()
            fake_file = fake_bin / "file"
            fake_file.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Mach-O 64-bit executable arm64'\n",
                encoding="utf-8",
            )
            fake_file.chmod(0o755)

            bundle_name = "WeishushuDev" if arguments == ["--dev"] else "Weishushu"
            executable = (
                probe_root
                / "dist"
                / f"{bundle_name}.app"
                / "Contents"
                / "MacOS"
                / bundle_name
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"probe")

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "PROFILE_LOG": str(profile_log),
                    "PYTHON": str(fake_python),
                    "WEISHUSHU_PROFILE": initial_profile,
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(scripts_dir / script_name), *arguments],
                cwd=probe_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            return profile_log.read_text(encoding="utf-8")

    def _write_frozen_manifest(self, resources_dir: Path) -> None:
        from packaging.build_manifest import make_manifest, write_manifest

        write_manifest(
            resources_dir / "weishushu_build_manifest.json",
            make_manifest(
                app_version="2.0.0",
                source_commit="packaging123",
                platform="darwin",
                architecture="arm64",
                python_version="3.12.13",
                pyinstaller_version="6.0.0",
                dependency_lock_sha256="d" * 64,
                profile="user",
                executable_name="Weishushu",
                bundle_identifier="com.weishushu.desktop",
                resources=[],
            ),
        )

    def test_macos_frozen_uses_meipass_playwright_directory(self):
        from backend.app.services.setup_check import get_frozen_ms_playwright

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundled = root / "ms-playwright"
            bundled.mkdir()
            executable = (
                root / "Weishushu.app" / "Contents" / "MacOS" / "Weishushu"
            )
            self._write_frozen_manifest(
                root / "Weishushu.app" / "Contents" / "Resources"
            )
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "platform", "darwin"), \
                 patch.object(sys, "_MEIPASS", td, create=True), \
                 patch.object(sys, "executable", str(executable), create=True):
                self.assertEqual(get_frozen_ms_playwright(), bundled)

    def test_macos_frozen_extracts_browser_archive_to_cache(self):
        from backend.app.services.setup_check import configure_frozen_playwright_browsers_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source" / "chromium-test"
            source.mkdir(parents=True)
            (source / "browser-bin").write_text("browser", encoding="utf-8")
            archive = root / "playwright-browsers.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source, arcname="chromium-test")

            cache_root = root / "cache"
            paths = type("Paths", (), {"cache_dir": lambda self: cache_root})()
            executable = (
                root / "Weishushu.app" / "Contents" / "MacOS" / "Weishushu"
            )
            self._write_frozen_manifest(
                root / "Weishushu.app" / "Contents" / "Resources"
            )
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "platform", "darwin"), \
                 patch.object(sys, "_MEIPASS", str(root), create=True), \
                 patch.object(sys, "executable", str(executable), create=True), \
                 patch("backend.app.platform_paths.platform_paths", return_value=paths):
                self.assertEqual(configure_frozen_playwright_browsers_path(), cache_root / "ms-playwright")

            self.assertTrue((cache_root / "ms-playwright" / "chromium-test" / "browser-bin").exists())

    def test_macos_release_check_requires_mac_spec(self):
        from scripts import release_check

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "build_mac.spec").write_text("# mac build\n" * 30, encoding="utf-8")
            with patch("scripts.release_check.ROOT", root), \
                 patch("scripts.release_check.sys.platform", "darwin"):
                result = release_check.check_12_build_spec_exists()

        self.assertTrue(result.ok)
        self.assertIn("build_mac.spec", result.name)

    def test_mac_build_files_keep_required_resources_together(self):
        root = Path(__file__).resolve().parents[1]
        spec = root / "build_mac.spec"
        script = root / "scripts" / "build_mac.sh"

        self.assertTrue(spec.exists())
        self.assertTrue(script.exists())
        spec_source = spec.read_text(encoding="utf-8")
        script_source = script.read_text(encoding="utf-8")
        for token in (
            "from PyInstaller.utils.hooks import copy_metadata",
            'copy_metadata("crawl4weibo")',
            "backend/app/templates",
            "backend/app/static",
            "weibo_book/templates",
            "ms-playwright",
            "webview.platforms.cocoa",
            "BUNDLE(",
            "make_archive",
            "gztar",
            "desktop.browser.mac_webkit",
            '"AppKit"',
            '"Foundation"',
            '"WebKit"',
            '"PyObjCTools"',
        ):
            self.assertIn(token, spec_source)
        self.assertIn("playwright install chromium", script_source)
        self.assertIn("build_mac.spec", script_source)


    def test_mac_manifest_is_written_before_codesign_verify(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "build_mac.sh").read_text(encoding="utf-8")
        manifest_pos = source.find("scripts/write_build_manifest.py")
        codesign_verify_pos = source.find("codesign --verify --deep --strict")
        self.assertGreaterEqual(codesign_verify_pos, 0)
        self.assertGreater(manifest_pos, 0)
        self.assertGreater(codesign_verify_pos, manifest_pos)
        self.assertIn("--browser-archive playwright-browsers.tar.gz", source)
        # 禁止在构建脚本中固定 Chromium 可执行文件路径，必须从归档解析。
        self.assertNotIn("--extracted-browser chromium/chrome-headless-shell", source)

    def test_mac_build_requires_clean_worktree_before_removing_build_dirs(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "build_mac.sh").read_text(encoding="utf-8")
        gate_pos = source.find("git status --porcelain")
        head_pos = source.find("git rev-parse HEAD")
        rm_pos = source.find('rm -rf "build/')
        self.assertGreaterEqual(gate_pos, 0, "缺少干净工作树门禁")
        self.assertGreaterEqual(head_pos, 0, "缺少 HEAD 记录")
        self.assertGreaterEqual(rm_pos, 0)
        self.assertLess(gate_pos, rm_pos, "干净工作树门禁必须位于删除 build/dist 之前")
        self.assertIn("WEISHUSHU_SOURCE_COMMIT", source)

    def test_dmg_script_enforces_arm64_layout_and_checksum(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "build_dmg.sh"

        self.assertTrue(script.exists())
        source = script.read_text(encoding="utf-8")
        for token in (
            "Mach-O 64-bit executable arm64",
            "codesign --verify --deep --strict",
            "ln -s /Applications",
            "hdiutil create",
            "hdiutil attach",
            "hdiutil detach",
            "shasum -a 256",
            ".sha256",
        ):
            self.assertIn(token, source)

    def test_dev_bundle_runtime_identity_matches_profile_detection(self):
        root = Path(__file__).resolve().parents[1]
        spec_source = (root / "build_mac.spec").read_text(encoding="utf-8")
        profile_source = (root / "backend" / "app" / "profile.py").read_text(encoding="utf-8")

        self.assertIn('BUNDLE_NAME = "WeishushuDev" if _is_dev() else "Weishushu"', spec_source)
        self.assertIn('DEV_EXECUTABLE_NAME = "WeishushuDev"', profile_source)
        self.assertIn('executable_name == DEV_EXECUTABLE_NAME', profile_source)

    def test_dmg_missing_app_hint_uses_explicit_profile_branch(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts" / "build_dmg.sh").read_text(encoding="utf-8")

        self.assertNotIn("${DEV_MODE:+ --dev}", source)
        self.assertIn('BUILD_COMMAND="scripts/build_mac.sh"', source)
        self.assertIn('BUILD_COMMAND="scripts/build_mac.sh --dev"', source)

    @unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 验证 Bash 构建身份")
    def test_mac_build_scripts_clear_external_dev_profile_for_user_builds(self):
        for script_name in ("build_mac.sh", "build_dmg.sh"):
            with self.subTest(script=script_name, arguments=[]):
                self.assertEqual(
                    self._run_profile_probe(
                        script_name,
                        [],
                        initial_profile="dev",
                    ),
                    "",
                )
            with self.subTest(script=script_name, arguments=["--user"]):
                self.assertEqual(
                    self._run_profile_probe(
                        script_name,
                        ["--user"],
                        initial_profile="dev",
                    ),
                    "",
                )

    @unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 验证 Bash 构建身份")
    def test_mac_build_scripts_set_dev_profile_only_for_explicit_dev(self):
        for script_name in ("build_mac.sh", "build_dmg.sh"):
            with self.subTest(script=script_name):
                self.assertEqual(
                    self._run_profile_probe(
                        script_name,
                        ["--dev"],
                        initial_profile="user",
                    ),
                    "dev",
                )

    @unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 验证 Bash 构建身份")
    def test_mac_build_scripts_reject_unknown_profile_argument(self):
        root = Path(__file__).resolve().parents[1]
        for script_name in ("build_mac.sh", "build_dmg.sh"):
            with self.subTest(script=script_name):
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(root / "scripts" / script_name),
                        "--unknown",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("[ERROR] 未知参数: --unknown", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 验证 Bash 构建身份")
    def test_mac_build_scripts_reject_conflicting_profile_arguments(self):
        root = Path(__file__).resolve().parents[1]
        for script_name in ("build_mac.sh", "build_dmg.sh"):
            with self.subTest(script=script_name):
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(root / "scripts" / script_name),
                        "--dev",
                        "--user",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("[ERROR] 只能指定一个构建身份参数", result.stdout)


if __name__ == "__main__":
    unittest.main()
