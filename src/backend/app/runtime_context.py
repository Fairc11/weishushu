"""封包运行时不可变上下文。

所有源码态/冻结态路径与身份解析统一从这里产生；业务模块不再自行拼接
应用目录、Cookie 文件名或可执行文件名。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.app.profile import (
    DEV_PROFILE,
    app_dirname,
    app_executable_name,
    app_profile,
    bundle_identifier,
    configure_source_profile,
    default_cookie_filename,
)

RUNTIME_SOURCE = "source"
RUNTIME_FROZEN = "frozen"
PROFILE_DEV = DEV_PROFILE
PROFILE_USER = "user"
PLATFORM_DARWIN = "darwin"
PLATFORM_WIN32 = "win32"

SELF_TEST_ROOT_ENV = "WEISHUSHU_SELF_TEST_ROOT"
EXTERNAL_PROFILE_ENV = "WEISHUSHU_PROFILE"


class RuntimeContextError(RuntimeError):
    """运行时上下文解析失败。"""


@dataclass(frozen=True)
class RuntimeContext:
    run_mode: str
    profile: str
    platform: str
    executable_root: Path
    resource_root: Path
    data_root: Path
    cache_root: Path
    log_root: Path
    state_root: Path
    output_root: Path
    cookie_file: Path
    browser_storage_root: Path
    console_available: bool
    app_version: str
    source_commit: str
    manifest_path: Path
    self_test_root: Path | None
    executable_name: str
    bundle_identifier: str


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _platform() -> str:
    return sys.platform


def _executable_name() -> str:
    if _is_frozen():
        if sys.platform == "win32" and "\\" in str(sys.executable):
            from pathlib import PureWindowsPath
            return PureWindowsPath(str(sys.executable)).name
        return Path(sys.executable).name
    return Path(sys.executable).name if hasattr(sys, "executable") else "python"


def _profile() -> str:
    if _is_frozen():
        name = _executable_name()
        if sys.platform == "win32":
            if name != "Weishushu.exe":
                raise RuntimeContextError(f"Windows frozen 不接受可执行文件名: {name}")
            return PROFILE_USER
        if name == "WeishushuDev":
            return PROFILE_DEV
        if name == "Weishushu":
            return PROFILE_USER
        raise RuntimeContextError(f"frozen 只接受精确可执行文件名，当前为: {name}")
    # 源码入口必须由 run.py 先调用 configure_source_profile()；若未配置也强制 dev，
    # 避免源码态读取个人正式目录。
    return PROFILE_DEV


def _source_commit() -> str:
    # 构建清单或环境提供；普通源码环境不读取 Git。
    return os.environ.get("WEISHUSHU_SOURCE_COMMIT", "unknown")


def _frozen_build_identity(
    manifest_path: Path,
    platform: str,
    profile: str,
    executable_name: str,
    bundle_id: str,
) -> tuple[str, str]:
    """frozen 身份只来自构建清单；缺失、unknown 或不一致一律失败关闭。"""
    if not manifest_path.is_file():
        raise RuntimeContextError(f"frozen 缺少构建清单: {manifest_path}")
    from packaging.build_manifest import BuildManifestError, read_manifest

    try:
        manifest = read_manifest(manifest_path)
    except BuildManifestError as exc:
        raise RuntimeContextError(f"构建清单无效: {exc}") from exc
    if manifest["platform"] != platform:
        raise RuntimeContextError(
            f"清单平台 {manifest['platform']} 与运行平台 {platform} 不一致"
        )
    if manifest["profile"] != profile:
        raise RuntimeContextError(
            f"清单 profile {manifest['profile']} 与运行身份 {profile} 不一致"
        )
    if manifest["executable_name"] != executable_name:
        raise RuntimeContextError(
            f"清单可执行文件名 {manifest['executable_name']} 与实际 {executable_name} 不一致"
        )
    if manifest["bundle_identifier"] != bundle_id:
        raise RuntimeContextError(
            f"清单 Bundle ID {manifest['bundle_identifier']} 与运行身份 {bundle_id} 不一致"
        )
    return str(manifest["app_version"]), str(manifest["source_commit"])


def _manifest_path(
    resource_root: Path,
    platform: str,
    run_mode: str,
    executable_root: Path | None = None,
) -> Path:
    if run_mode == RUNTIME_FROZEN and platform == PLATFORM_DARWIN and executable_root is not None:
        # .app 清单位于 Contents/Resources，而不是 PyInstaller 的 _MEIPASS/Frameworks。
        return executable_root.parent / "Resources" / "weishushu_build_manifest.json"
    return resource_root / "weishushu_build_manifest.json"


def _self_test_root() -> Path | None:
    raw = os.environ.get(SELF_TEST_ROOT_ENV)
    if not raw:
        return None
    return Path(raw).resolve()


def resolve_runtime_context() -> RuntimeContext:
    """解析当前运行形态的不可变上下文；失败时保留原始异常并停止。"""
    run_mode = RUNTIME_FROZEN if _is_frozen() else RUNTIME_SOURCE
    platform = _platform()
    profile = _profile()

    # 自检根必须先于任何用户目录解析：自检模式不得触碰 Path.home、
    # LOCALAPPDATA 或正式用户目录。
    self_test = _self_test_root()
    if self_test is not None and run_mode != RUNTIME_FROZEN:
        raise RuntimeContextError("普通源码启动不得接受 WEISHUSHU_SELF_TEST_ROOT")

    if run_mode == RUNTIME_SOURCE:
        configure_source_profile()
        executable_root = Path(__file__).resolve().parents[2]  # repo root
        resource_root = executable_root
        data_root = executable_root / ".run"
        cache_root = data_root / "cache"
        log_root = data_root / "logs"
        state_root = data_root / "state"
        output_root = executable_root / "output"
        browser_storage_root = state_root / "browser"
        manifest_path = _manifest_path(resource_root, platform, run_mode, executable_root)
        app_version = "2.0.1"
        source_commit = _source_commit()
    else:
        executable_root = Path(sys.executable).parent
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            resource_root = Path(meipass)
        elif platform == PLATFORM_DARWIN:
            resource_root = executable_root.parent / "Resources"
        else:
            resource_root = executable_root / "_internal"
        manifest_path = _manifest_path(resource_root, platform, run_mode, executable_root)
        app_version, source_commit = _frozen_build_identity(
            manifest_path,
            platform,
            profile,
            _executable_name(),
            bundle_identifier(),
        )
        if self_test is not None:
            data_root = self_test / "data"
            cache_root = self_test / "cache"
            log_root = self_test / "logs"
            state_root = self_test / "state"
            output_root = self_test / "output"
            browser_storage_root = self_test / "browser"
        else:
            data_root = _user_data_root(platform, profile)
            cache_root = _user_cache_root(platform, profile, data_root)
            log_root = _user_log_root(platform, profile, data_root)
            state_root = data_root / "state"
            output_root = data_root / "output"
            browser_storage_root = state_root / "browser"

    if self_test is not None:
        cookie_file = self_test / "cookies.json"
    else:
        cookie_file = state_root / default_cookie_filename()

    return RuntimeContext(
        run_mode=run_mode,
        profile=profile,
        platform=platform,
        executable_root=executable_root,
        resource_root=resource_root,
        data_root=data_root,
        cache_root=cache_root,
        log_root=log_root,
        state_root=state_root,
        output_root=output_root,
        cookie_file=cookie_file,
        browser_storage_root=browser_storage_root,
        console_available=bool(sys.stdout is not None and sys.stderr is not None),
        app_version=app_version,
        source_commit=source_commit,
        manifest_path=manifest_path,
        self_test_root=self_test,
        executable_name=_executable_name(),
        bundle_identifier=bundle_identifier(),
    )


def _env_dir(name: str, fallback: str) -> Path:
    """先读环境变量；仅在为空时才求值 Path.home 兜底。"""
    value = os.environ.get(name)
    if value:
        return Path(value)
    return Path(fallback)


def _user_data_root(platform: str, profile: str) -> Path:
    dirname = app_dirname()
    if platform == PLATFORM_DARWIN:
        return Path.home() / "Library" / "Application Support" / dirname
    if platform == PLATFORM_WIN32:
        localappdata = os.environ.get("LOCALAPPDATA")
        if not localappdata:
            localappdata = str(Path.home() / "AppData" / "Local")
        return Path(localappdata) / dirname
    xdg = os.environ.get("XDG_DATA_HOME")
    if not xdg:
        xdg = str(Path.home() / ".local" / "share")
    return Path(xdg) / dirname


def _user_cache_root(platform: str, profile: str, data_root: Path) -> Path:
    if platform == PLATFORM_DARWIN:
        return Path.home() / "Library" / "Caches" / app_dirname()
    return data_root / "cache"


def _user_log_root(platform: str, profile: str, data_root: Path) -> Path:
    if platform == PLATFORM_DARWIN:
        return Path.home() / "Library" / "Logs" / app_dirname()
    return data_root / "logs"
