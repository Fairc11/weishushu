"""启动真实首页并验证 iOS 27 染色玻璃的关键视觉状态。"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
PORT = 18083
URL = f"http://127.0.0.1:{PORT}/"
OUTPUT_DIR = Path("/tmp/weishushu-ios27-tinted-glass")
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 960},
    "narrow": {"width": 900, "height": 900},
}


def wait_for_server(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Uvicorn 提前退出，退出码：{process.returncode}")
        try:
            with urlopen(URL, timeout=1) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.2)
    raise TimeoutError("本地 FastAPI 在 15 秒内未响应")


def open_home(context: BrowserContext, page: Page | None = None) -> Page:
    page = page or context.new_page()
    page.goto(URL, wait_until="networkidle")

    risk_overlay = page.locator("#risk-modal-overlay")
    if risk_overlay.is_visible():
        page.locator("#risk-scroll").evaluate("el => { el.scrollTop = el.scrollHeight }")
        page.locator("#risk-confirm-btn").click()
        risk_overlay.wait_for(state="hidden")

    for selector, label in (
        ("#url-input", "链接输入框"),
        ("#btn-search-blogger", "搜索按钮"),
        ("#login-menu-toggle", "登录状态按钮"),
    ):
        if not page.locator(selector).is_visible():
            raise AssertionError(f"首页未显示{label}")
    if page.locator("#browser-area").is_visible():
        raise AssertionError("首页默认显示了浏览器工具区")
    if page.locator("#log-toggle").count():
        raise AssertionError("首页默认显示了任务日志入口")

    page.add_style_tag(
        content=(
            ".history-panel { "
            "backdrop-filter: var(--ios-glass-blur); "
            "-webkit-backdrop-filter: var(--ios-glass-blur); "
            "}"
        )
    )
    return page


def save(page: Page, name: str) -> None:
    output = OUTPUT_DIR / f"{name}.png"
    page.screenshot(path=str(output), full_page=False)
    print(f"saved: {output}")


def disable_backdrop_for_structure_capture(page: Page) -> None:
    page.add_style_tag(
        content=(
            "* { backdrop-filter: none !important; "
            "-webkit-backdrop-filter: none !important; }"
        )
    )


def capture_default(browser: Browser, name: str, viewport: dict[str, int]) -> None:
    context = browser.new_context(viewport=viewport)
    try:
        page = open_home(context)
        if name == "desktop":
            page.locator(".app-header").evaluate(
                "element => { "
                "element.style.backdropFilter = 'none'; "
                "element.style.webkitBackdropFilter = 'none'; "
                "}"
            )
        save(page, name)
    finally:
        context.close()


def capture_dark(browser: Browser) -> None:
    context = browser.new_context(viewport=VIEWPORTS["desktop"])
    context.add_init_script("localStorage.setItem('weishushu.theme', 'dark')")
    try:
        page = open_home(context)
        if page.locator("html").get_attribute("data-theme") != "dark":
            raise AssertionError("深色主题没有生效")
        save(page, "dark")
    finally:
        context.close()


def capture_open_panel(browser: Browser, trigger: str, panel: str, name: str) -> None:
    context = browser.new_context(viewport=VIEWPORTS["desktop"])
    try:
        page = open_home(context)
        page.locator(trigger).click()
        panel_locator = page.locator(panel)
        if not panel_locator.is_visible():
            raise AssertionError(f"{name} 未在点击后显示")
        panel_locator.evaluate(
            "element => Promise.all(element.getAnimations().map(item => item.finished))"
        )
        if name == "history-panel":
            if not page.locator("#history-close").is_visible():
                raise AssertionError("历史抽屉的关闭按钮不可见")
            if panel_locator.bounding_box()["y"] != 0:
                raise AssertionError("历史抽屉没有覆盖到窗口顶部")
            backdrop_filter = panel_locator.evaluate(
                "element => getComputedStyle(element).backdropFilter"
            )
            if backdrop_filter != "none":
                raise AssertionError(
                    f"历史抽屉启用了全高背景模糊：{backdrop_filter}"
                )
            disable_backdrop_for_structure_capture(page)
        save(page, name)
        if name == "history-panel":
            page.locator("#history-close").click()
            if not panel_locator.is_hidden():
                raise AssertionError("历史抽屉点击关闭后仍然可见")
            parent_class = panel_locator.evaluate("element => element.parentElement.className")
            if "steps" not in parent_class.split():
                raise AssertionError("历史抽屉关闭后没有回到主内容容器")
    finally:
        context.close()


def capture_reduced_transparency(browser: Browser) -> None:
    context = browser.new_context(viewport=VIEWPORTS["desktop"])
    try:
        page = context.new_page()
        session = context.new_cdp_session(page)
        session.send(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-reduced-transparency", "value": "reduce"}
                ]
            },
        )
        page = open_home(context, page)
        backdrop_filter = page.locator(".app-header").evaluate(
            "element => getComputedStyle(element).backdropFilter"
        )
        if backdrop_filter != "none":
            raise AssertionError(
                f"降低透明度后顶栏仍有模糊效果：{backdrop_filter}"
            )
        save(page, "reduced-transparency")
    finally:
        context.close()


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server(process)
        with sync_playwright() as playwright:
            # 每个状态使用独立 GPU 进程，避免 backdrop-filter 合成层相互污染。
            for name, viewport in VIEWPORTS.items():
                browser = playwright.chromium.launch(headless=True)
                try:
                    capture_default(browser, name, viewport)
                finally:
                    browser.close()

            captures = (
                lambda browser: capture_dark(browser),
                lambda browser: capture_open_panel(
                    browser, "#login-menu-toggle", "#login-menu", "login-menu"
                ),
                lambda browser: capture_open_panel(
                    browser, "#history-toggle", "#history-panel", "history-panel"
                ),
                lambda browser: capture_reduced_transparency(browser),
            )
            for capture in captures:
                browser = playwright.chromium.launch(headless=True)
                try:
                    capture(browser)
                finally:
                    browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
