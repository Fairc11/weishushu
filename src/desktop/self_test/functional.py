"""十一项无界面封包功能自检。

真实封包中由 run.py 的 `--packaged-self-test` 调用；源码单元测试可注入
context 或直接调用步骤构造器。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from backend.app.runtime_context import RuntimeContext
from desktop.self_test.network_guard import assert_loopback
from desktop.self_test.schema import (
    STATUS_FAILED,
    STATUS_PASSED,
    add_step,
    new_result,
    set_error,
    step,
    write_result,
)

FUNCTIONAL_STEPS = (
    "manifest_identity",
    "writable_paths_isolated",
    "fastapi_health",
    "static_assets",
    "chromium_launch",
    "cookie_isolated",
    "login_contract",
    "media_download",
    "archive_generate",
    "output_isolated",
    "json_saved",
)


def error_kind_for_step(name: str) -> str:
    """最早失败步骤到统一错误分类的映射。"""
    mapping = {
        "manifest_identity": "manifest",
        "writable_paths_isolated": "filesystem",
        "output_isolated": "filesystem",
        "fastapi_health": "resource",
        "static_assets": "resource",
        "chromium_launch": "browser",
        "cookie_isolated": "browser",
        "login_contract": "login_contract",
        "media_download": "media",
        "archive_generate": "archive",
    }
    return mapping.get(name, "unknown")

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _local_http_server(root: Path) -> Iterator[str]:
    """在 127.0.0.1 启动只读本地 HTTP 服务，返回 base URL。"""
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def _manifest_step(context: RuntimeContext) -> dict[str, Any]:
    manifest_path = context.manifest_path
    if not manifest_path.is_file():
        return step("manifest_identity", STATUS_FAILED, message=f"清单不存在: {manifest_path}")
    try:
        from packaging.build_manifest import read_manifest, verify_resources

        manifest = read_manifest(manifest_path)
        errors = verify_resources(context.resource_root, manifest)
        if errors:
            return step("manifest_identity", STATUS_FAILED, message="; ".join(errors))
        return step("manifest_identity", STATUS_PASSED, message="清单与资源哈希一致")
    except Exception as exc:
        return step("manifest_identity", STATUS_FAILED, message=f"清单校验失败: {exc}")


def _paths_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("writable_paths_isolated", STATUS_FAILED, message="未设置自检临时根")
    root = context.self_test_root
    writable = (
        context.data_root,
        context.cache_root,
        context.log_root,
        context.state_root,
        context.output_root,
    )
    if not all(path.is_relative_to(root) for path in writable):
        return step("writable_paths_isolated", STATUS_FAILED, message="存在越出自检根的可写路径")
    return step("writable_paths_isolated", STATUS_PASSED, message="可写路径全部位于自检根")


def _static_step(context: RuntimeContext) -> dict[str, Any]:
    static = context.resource_root / "backend" / "app" / "static"
    templates = context.resource_root / "backend" / "app" / "templates"
    if static.is_dir() and templates.is_dir():
        return step("static_assets", STATUS_PASSED, message="静态与模板资源存在")
    return step("static_assets", STATUS_FAILED, message="静态或模板资源缺失")


def _fastapi_step() -> dict[str, Any]:
    """启动真实回环 FastAPI 服务，而不是 TestClient。"""
    server = None
    thread = None
    try:
        import httpx
        import uvicorn
        from backend.app.main import app

        port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            return step("fastapi_health", STATUS_FAILED, message="FastAPI 未进入监听")
        url = f"http://127.0.0.1:{port}/healthz"
        assert_loopback(url)
        response = httpx.get(url, timeout=5)
        if response.status_code == 200:
            return step("fastapi_health", STATUS_PASSED, message="/healthz 返回 200")
        return step("fastapi_health", STATUS_FAILED, message=f"/healthz 返回 {response.status_code}")
    except Exception as exc:
        return step("fastapi_health", STATUS_FAILED, message=f"FastAPI 启动失败: {exc}")
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=2)


def _resolve_browser_executable(context: RuntimeContext) -> Path | None:
    """统一解析 chromium_launch/cookie_isolated 的 Chromium 可执行路径。

    清单不存在时返回 None，保留源码测试默认 Playwright；
    清单存在时要求有效 extracted_browser，缺失/非法/根不存在/文件不存在
    一律抛异常，不允许静默回退到 runner 外部 Chromium。
    """
    if not context.manifest_path.is_file():
        return None
    from packaging.build_manifest import read_manifest

    manifest = read_manifest(context.manifest_path)
    extracted = manifest.get("extracted_browser")
    if not isinstance(extracted, dict) or not extracted.get("expected_relative_path"):
        raise ValueError("清单缺少 extracted_browser")
    location = extracted.get("location") or "cache"
    expected_rel = extracted["expected_relative_path"]
    if location == "cache":
        browser_root = context.cache_root / "ms-playwright"
    elif location == "bundle":
        from backend.app.services.setup_check import get_frozen_ms_playwright

        browser_root = get_frozen_ms_playwright()
        if browser_root is None:
            raise ValueError("封包内置浏览器根不存在")
    else:
        raise ValueError(f"未知 extracted_browser.location: {location}")
    browser_root = browser_root.resolve()
    if not browser_root.is_dir():
        raise ValueError(f"浏览器根不存在: {browser_root}")
    expected_path = (browser_root / expected_rel).resolve()
    if not expected_path.is_file():
        raise ValueError(f"清单 Chromium 不存在: {expected_path}")
    if not expected_path.is_relative_to(browser_root):
        raise ValueError(f"Chromium 路径越出浏览器根: {expected_path}")
    return expected_path


def _chromium_step(context: RuntimeContext) -> dict[str, Any]:
    """启动清单指定的 Chromium 可执行文件并访问本地回环页面。

    chromium_launch 与 cookie_isolated 共用 _resolve_browser_executable，
    确保两个步骤核对同一清单路径并在 bundle/cache 下使用准确 executable_path。
    """
    try:
        from playwright.sync_api import sync_playwright

        expected_path = _resolve_browser_executable(context)
        launch_kwargs = {"headless": True}
        if expected_path is not None:
            launch_kwargs["executable_path"] = str(expected_path)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                with _local_http_server(FIXTURES_ROOT) as base_url:
                    page = browser.new_page()
                    def block_external(route):
                        from urllib.parse import urlparse
                        host = urlparse(route.request.url).hostname or ""
                        from desktop.self_test.network_guard import is_loopback_host
                        if not is_loopback_host(host):
                            route.abort()
                        else:
                            route.continue_()
                    page.route("**/*", block_external)
                    assert_loopback(base_url)
                    page.goto(f"{base_url}/index.html", wait_until="load")
                    if "微书薯自检" not in page.title():
                        return step("chromium_launch", STATUS_FAILED, message="本地页面标题不匹配")
                    actual = Path(launch_kwargs.get("executable_path") or playwright.chromium.executable_path)
                    if expected_path is not None and actual.resolve() != expected_path:
                        return step(
                            "chromium_launch",
                            STATUS_FAILED,
                            message=f"Chromium 路径与清单不符: {actual} != {expected_path}",
                        )
                    return step("chromium_launch", STATUS_PASSED, message="Chromium 已访问本地页面")
            finally:
                browser.close()
    except Exception as exc:
        return step("chromium_launch", STATUS_FAILED, message=f"Chromium 启动失败: {exc}")


def _cookie_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("cookie_isolated", STATUS_FAILED, message="未设置自检临时根")
    try:
        from playwright.sync_api import sync_playwright

        expected_path = _resolve_browser_executable(context)
        launch_kwargs = {"headless": True}
        if expected_path is not None:
            launch_kwargs["executable_path"] = str(expected_path)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                context_page = browser.new_context()
                context_page.add_cookies(
                    [
                        {
                            "name": "weishushu_self_test",
                            "value": "ok",
                            "domain": "127.0.0.1",
                            "path": "/",
                        }
                    ]
                )
                cookies = context_page.cookies()
                if not any(c["name"] == "weishushu_self_test" and c["value"] == "ok" for c in cookies):
                    return step("cookie_isolated", STATUS_FAILED, message="隔离 Cookie 写读失败")
                context_page.clear_cookies()
                if any(c["name"] == "weishushu_self_test" for c in context_page.cookies()):
                    return step("cookie_isolated", STATUS_FAILED, message="隔离 Cookie 删除失败")
                context_page.close()
                return step("cookie_isolated", STATUS_PASSED, message="隔离浏览器 Cookie 写读删通过")
            finally:
                browser.close()
    except Exception as exc:
        return step("cookie_isolated", STATUS_FAILED, message=f"浏览器 Cookie 失败: {exc}")


def _login_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("login_contract", STATUS_FAILED, message="未设置自检临时根")
    fixture = FIXTURES_ROOT / "login.json"
    cookies_fixture = FIXTURES_ROOT / "cookies.json"
    if not fixture.is_file() or not cookies_fixture.is_file():
        return step("login_contract", STATUS_FAILED, message="缺少离线账号/cookie fixture")
    try:
        import weibo_book.login as login

        payload = json.loads(fixture.read_text(encoding="utf-8"))
        cookies = json.loads(cookies_fixture.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "uid" not in payload or "screen_name" not in payload:
            return step("login_contract", STATUS_FAILED, message="账号 fixture 缺少契约字段")
        if not cookies:
            return step("login_contract", STATUS_FAILED, message="cookie fixture 为空")
        # 走生产 Cookie 持久化与头部契约；在自检临时根内写入。
        login.save_cookies(cookies, str(context.cookie_file))
        stored = login.load_cookies(str(context.cookie_file))
        cookie_list = stored.get("cookies", [])
        header = login.cookies_to_header_for_host(cookie_list, "m.weibo.cn")
        if not header:
            return step("login_contract", STATUS_FAILED, message="生产 Cookie 头部契约为空")
        return step(
            "login_contract",
            STATUS_PASSED,
            message=f"离线账号契约通过 uid={payload['uid']}",
        )
    except Exception as exc:
        return step("login_contract", STATUS_FAILED, message=f"离线账号契约失败: {exc}")


def _media_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("media_download", STATUS_FAILED, message="未设置自检临时根")
    fixture = FIXTURES_ROOT / "media" / "sample.bin"
    if not fixture.is_file():
        return step("media_download", STATUS_FAILED, message="缺少媒体 fixture")
    try:
        from weibo_book.media import MediaDownloader
        from weibo_book.models import MediaType, Post, PostMedia

        with _local_http_server(FIXTURES_ROOT / "media") as base_url:
            url = f"{base_url}/sample.bin"
            assert_loopback(url)
            post = Post(
                bid="fixture001",
                uid="10000000",
                user_name="自检用户",
                user_avatar="",
                text="离线媒体自检",
                media=[PostMedia(type=MediaType.VIDEO, url=url)],
            )
            downloader = MediaDownloader(context.output_root, max_workers=1)
            result = downloader.download_all([post])
            if result.get("success", 0) < 1:
                return step("media_download", STATUS_FAILED, message=f"生产媒体下载未成功: {result}")
            destination = context.output_root / "media"
            downloaded = list(destination.rglob("*"))
            if not any(path.is_file() and path.read_bytes() == fixture.read_bytes() for path in downloaded):
                return step("media_download", STATUS_FAILED, message="未找到与 fixture 一致的媒体字节")
            return step("media_download", STATUS_PASSED, message="生产媒体下载入口通过")
    except Exception as exc:
        return step("media_download", STATUS_FAILED, message=f"媒体下载失败: {exc}")


def _archive_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("archive_generate", STATUS_FAILED, message="未设置自检临时根")
    try:
        from datetime import datetime, timezone

        from weibo_book.archive.discovery import ProfileItem, ProfilePage
        from weibo_book.archive.render_snapshot import ArchiveRenderer
        from weibo_book.archive.repository import ArchiveRepository
        from weibo_book.archive.sync import PersonalArchiveSync
        from weibo_book.models import Post

        class _IdentityProvider:
            def whoami(self) -> dict:
                return {"uid": "10000000", "screen_name": "自检用户"}

        class _OfflineSource:
            def iter_profile_pages(self, uid: str, **_kwargs):
                assert uid == "10000000"
                yield ProfilePage([ProfileItem("fixture001")], is_last=True)

            def fetch_post(self, uid: str, bid: str) -> Post:
                assert uid == "10000000"
                return Post(
                    bid=bid,
                    uid=uid,
                    user_name="自检用户",
                    user_avatar="",
                    text="离线最小档案自检",
                    created_at=datetime(2026, 7, 14, 1, 2, 3, tzinfo=timezone.utc),
                )

            def fetch_recent_comments(self, post_id: str, limit: int = 10) -> list:
                return []

        root = context.output_root / "archive"
        if root.exists():
            import shutil
            shutil.rmtree(root)
        sync = PersonalArchiveSync(
            root,
            _OfflineSource(),
            _IdentityProvider(),
        )
        result = sync.run("create")
        repository = ArchiveRepository.open(root, "10000000")
        try:
            ArchiveRenderer(repository).render_all(
                root,
                render_pdf=lambda _html, pdf: pdf.write_bytes(b"pdf"),
            )
            if not (root / "微博书.html").is_file():
                return step("archive_generate", STATUS_FAILED, message="互动 HTML 未生成")
            return step(
                "archive_generate",
                STATUS_PASSED,
                message=f"最小档案生成通过 new_posts={result.new_posts}",
            )
        finally:
            repository.close()
    except Exception as exc:
        return step("archive_generate", STATUS_FAILED, message=f"最小档案失败: {exc}")


def _output_step(context: RuntimeContext) -> dict[str, Any]:
    if context.self_test_root is None:
        return step("output_isolated", STATUS_FAILED, message="未设置自检临时根")
    marker = context.output_root / "self-test-marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok", encoding="utf-8")
    if marker.is_relative_to(context.self_test_root):
        return step("output_isolated", STATUS_PASSED, message="生成物位于临时输出根")
    return step("output_isolated", STATUS_FAILED, message="生成物越出临时输出根")


def run_functional_self_test(
    context: RuntimeContext,
    output_path: Path,
) -> dict[str, Any]:
    result = new_result(
        build_commit=context.source_commit,
        profile=context.profile,
        platform=context.platform,
    )
    add_step(result, _manifest_step(context))
    add_step(result, _paths_step(context))
    add_step(result, _fastapi_step())
    add_step(result, _static_step(context))
    add_step(result, _chromium_step(context))
    add_step(result, _cookie_step(context))
    add_step(result, _login_step(context))
    add_step(result, _media_step(context))
    add_step(result, _archive_step(context))
    add_step(result, _output_step(context))
    add_step(result, step("json_saved", STATUS_PASSED, message="JSON 已原子写入"))

    failed = [item for item in result["steps"] if item["status"] == STATUS_FAILED]
    if failed:
        first_failed = failed[0]["name"]
        set_error(result, error_kind_for_step(first_failed), f"最早失败步骤: {first_failed}")
    else:
        result["error_kind"] = None
    write_result(output_path, result)
    return result
