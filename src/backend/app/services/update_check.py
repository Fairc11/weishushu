"""检查公开仓库 Release 新版本。

- GET /api/update/check 比对当前版本与最新 Release tag。
- 自动安装（下载 DMG、校验 sha256、替换 .app）仅在本机 frozen 正式版（macOS）开放；
  开发版与源码运行只提示新版本，不执行替换。

真实替换由退出后执行的守护脚本完成：先等当前进程退出，再把旧版移入废纸篓、
新版就位并重新打开，避免替换正在运行的 .app。
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import httpx

from backend.app import platform_paths
from backend.app.profile import app_profile
from backend.app.version import VERSION
from weibo_book.errors import WeiboError, WeiboErrorKind

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/Fairc11/weishushu/releases/latest"
RELEASES_PAGE = "https://github.com/Fairc11/weishushu/releases"
_REQUEST_HEADERS = {
    "User-Agent": "weishushu-update-check",
    "Accept": "application/vnd.github+json",
}


def parse_version(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", (value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    newest, current_parts = parse_version(latest), parse_version(current)
    if newest is None or current_parts is None:
        return False
    length = max(len(newest), len(current_parts))
    newest += (0,) * (length - len(newest))
    current_parts += (0,) * (length - len(current_parts))
    return newest > current_parts


def pick_mac_dmg(assets: list[dict]) -> dict | None:
    for asset in assets:
        name = asset.get("name", "")
        if name.endswith(".dmg") and "macOS" in name and "arm64" in name:
            return asset
    return None


def sha256_asset_for(assets: list[dict], dmg_name: str) -> dict | None:
    for asset in assets:
        if asset.get("name") == dmg_name + ".sha256":
            return asset
    return None


def bundle_path() -> Path | None:
    """frozen .app 的 bundle 路径；非 frozen 或布局不符时返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents":
        return None
    bundle = executable.parent.parent.parent
    return bundle if bundle.suffix == ".app" else None


def can_auto_install() -> bool:
    return (
        getattr(sys, "frozen", False)
        and app_profile() == ""
        and sys.platform == "darwin"
        and bundle_path() is not None
    )


def _release_payload() -> dict:
    try:
        response = httpx.get(
            RELEASES_API, headers=_REQUEST_HEADERS, timeout=15, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise WeiboError(
            "检查更新失败：网络连接异常", kind=WeiboErrorKind.NETWORK, original=exc
        ) from exc
    if response.status_code != 200:
        raise WeiboError(
            f"检查更新失败：发布页返回 {response.status_code}", kind=WeiboErrorKind.API
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise WeiboError("检查更新失败：返回内容无法解析", kind=WeiboErrorKind.PARSE) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tag_name"), str):
        raise WeiboError("检查更新失败：返回内容缺少版本信息", kind=WeiboErrorKind.PARSE)
    return payload


def check_latest_release() -> dict:
    payload = _release_payload()
    tag = payload["tag_name"]
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    dmg = pick_mac_dmg(assets)
    return {
        "current": VERSION,
        "latest": tag,
        "has_update": is_newer(tag, VERSION),
        "notes": payload.get("body") or "",
        "html_url": payload.get("html_url") or RELEASES_PAGE,
        "published_at": payload.get("published_at") or "",
        "can_auto_install": can_auto_install(),
        "dmg_asset": dmg["name"] if dmg else None,
        "releases_page": RELEASES_PAGE,
    }


def _download(client: httpx.Client, url: str, target: Path) -> None:
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise WeiboError(
                f"下载更新包失败：服务器返回 {response.status_code}",
                kind=WeiboErrorKind.API,
            )
        with target.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], error_message: str) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f"：{detail[-1]}" if detail else ""
        raise WeiboError(f"{error_message}{suffix}", kind=WeiboErrorKind.UNKNOWN)
    return result


def stage_latest_release() -> dict:
    """下载最新 DMG、校验 sha256、挂载并暂存到 bundle 同级目录，返回暂存信息。"""
    if not can_auto_install():
        raise WeiboError(
            "只有 macOS 正式安装版支持自动更新；请前往发布页手动下载",
            kind=WeiboErrorKind.API, recoverable=False,
        )
    info = check_latest_release()
    if not info["has_update"]:
        raise WeiboError("当前已是最新版本", kind=WeiboErrorKind.API, recoverable=False)
    payload = _release_payload()
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    dmg = pick_mac_dmg(assets)
    if dmg is None or not isinstance(dmg.get("browser_download_url"), str):
        raise WeiboError("最新版本缺少 macOS 安装包，请前往发布页手动下载", kind=WeiboErrorKind.PARSE)
    sha_asset = sha256_asset_for(assets, dmg["name"])
    if sha_asset is None or not isinstance(sha_asset.get("browser_download_url"), str):
        raise WeiboError("最新版本缺少校验文件，已停止自动更新", kind=WeiboErrorKind.PARSE)

    work_dir = platform_paths.ensure_dir(
        platform_paths.platform_paths().cache_dir() / "update" / payload["tag_name"]
    )
    dmg_path = work_dir / dmg["name"]
    sha_path = work_dir / sha_asset["name"]
    with httpx.Client(headers=_REQUEST_HEADERS, timeout=600, follow_redirects=True) as client:
        _download(client, sha_asset["browser_download_url"], sha_path)
        _download(client, dmg["browser_download_url"], dmg_path)

    sha_text = sha_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\b([0-9a-fA-F]{64})\b", sha_text)
    if match is None:
        raise WeiboError("校验文件内容无法识别，已停止自动更新", kind=WeiboErrorKind.PARSE)
    expected = match.group(1).lower()
    actual = _sha256_of(dmg_path)
    if actual != expected:
        raise WeiboError("更新包校验不一致，已停止自动更新", kind=WeiboErrorKind.PARSE, recoverable=False)

    mount = work_dir / "mnt"
    mount.mkdir(exist_ok=True)
    bundle = bundle_path()
    staged = bundle.parent / f"Weishushu-update-{int(time.time())}.app"
    try:
        _run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg_path)],
            "挂载更新包失败",
        )
        source = mount / "Weishushu.app"
        if not source.is_dir():
            raise WeiboError("更新包内未找到应用，已停止自动更新", kind=WeiboErrorKind.PARSE)
        _run(["ditto", str(source), str(staged)], "暂存新版本失败")
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount), "-quiet"], capture_output=True
        )
    _run(["codesign", "--verify", "--deep", "--strict", str(staged)], "新版本签名校验失败")
    return {
        "staged_path": str(staged),
        "version": payload["tag_name"],
        "current": VERSION,
    }


def apply_staged_update(staged_path: str) -> dict:
    """生成退出后执行的替换脚本并启动；调用方随后应关闭应用窗口。"""
    bundle = bundle_path()
    if bundle is None or not can_auto_install():
        raise WeiboError("当前环境不支持自动更新", kind=WeiboErrorKind.API, recoverable=False)
    staged = Path(staged_path)
    if (
        staged.suffix != ".app"
        or not staged.name.startswith("Weishushu-update-")
        or staged.parent != bundle.parent
        or not staged.is_dir()
    ):
        raise WeiboError("暂存的更新无效，请重新检查更新", kind=WeiboErrorKind.PARSE)

    timestamp = int(time.time())
    trash = Path.home() / ".Trash" / f"Weishushu-before-{VERSION}-{timestamp}.app"
    counter = 1
    while trash.exists():
        trash = Path.home() / ".Trash" / f"Weishushu-before-{VERSION}-{timestamp}-{counter}.app"
        counter += 1
    script = platform_paths.ensure_dir(
        platform_paths.platform_paths().cache_dir() / "update"
    ) / f"apply-{timestamp}.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done\n"
        f"mv {shlex.quote(str(bundle))} {shlex.quote(str(trash))} || exit 1\n"
        f"ditto {shlex.quote(str(staged))} {shlex.quote(str(bundle))} || exit 1\n"
        f"open {shlex.quote(str(bundle))}\n"
        f"rm -f {shlex.quote(str(script))}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    subprocess.Popen(
        ["/bin/bash", str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("更新替换脚本已启动：%s -> %s", staged, bundle)
    return {"restarting": True, "version": staged.name}
