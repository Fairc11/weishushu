"""Mac 同层玻璃浏览器工作区契约。

覆盖：
- 首页渲染 app-split-view + main-area + splitter + browser-area + a11y 属性
- 右栏 7 按钮（打开 / 后退 / 前进 / 刷新 / 注入 / 复制 / 关闭）存在
- app.js 含 initSplitView / applySplitRatio / weishushu.splitRatio + 比例边界 [0.5, 0.78]
"""
import sys
import unittest
from pathlib import Path
from frontend_assets import frontend_bundle_asset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from backend.app.main import app


class SplitViewRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_split_view_shell_rendered(self):
        """分屏外壳：app-split-view + main-area + splitter + browser-area + a11y 属性"""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        for token in (
            "app-split-view",
            'id="main-area"',
            'id="splitter"',
            'id="browser-area"',
            'role="separator"',
            'aria-orientation="vertical"',
        ):
            self.assertIn(token, r.text, f"缺少分屏外壳 token: {token}")

    def test_browser_workspace_collapsed_by_default(self):
        """普通 Web 模式默认不让原生浏览器槽抢主流程宽度。"""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="app-main app-split-view browser-console-hidden"', r.text)
        self.assertIn('id="browser-area" hidden', r.text)

    def test_integrated_browser_controls_and_native_slot_rendered(self):
        r = self.client.get("/")
        for button_id in (
            "btn-browser-back",
            "btn-browser-forward",
            "btn-browser-refresh",
            "btn-browser-sync",
            "btn-browser-copy",
            "btn-browser-close",
            "native-browser-slot",
        ):
            self.assertIn(button_id, r.text, f"缺少右栏按钮 id: {button_id}")
        self.assertIn('class="browser-dock browser-glass"', r.text)

    def test_browser_status_badge_rendered(self):
        """右栏状态徽章（v1.2.0 stage4 B+ 新增）"""
        r = self.client.get("/")
        self.assertIn('id="browser-status"', r.text)


class SplitViewJsTests(unittest.TestCase):
    def test_integrated_workspace_js_hooks_exist(self):
        src = frontend_bundle_asset().read_text(encoding="utf-8")
        for token in (
            "initSplitView",
            "applySplitRatio",
            "setBrowserPanelVisible",
            "browserPanelVisible",
            "weishushu.splitRatio",
            "splitRatio: 0.62",
            "ResizeObserver",
            "syncNativeBrowserFrame",
            "setBrowserFrame",
            "Math.max(0.5",
            "Math.min(0.78",
        ):
            self.assertIn(token, src, f"app.js 缺少 splitter token: {token}")

    def test_no_nested_main_in_index(self):
        """index.html 不应嵌套 <main>（v1.1.6 之前 <main class="steps"> 在 base <main> 内）"""
        idx = (Path(__file__).resolve().parents[1] /
               "backend/app/templates/index.html").read_text(encoding="utf-8")
        # 简单计数：应该只有 1 个 <main ...> 标签（在 base.html）
        # index.html 不应该有 <main>
        self.assertNotIn("<main", idx, "index.html 不应含 <main> 标签（避免嵌套）")
        # 但应有 <div class="steps">
        self.assertIn('<div class="steps">', idx)

    def test_browser_panel_uses_integrated_glass_class(self):
        idx = (Path(__file__).resolve().parents[1] /
               "backend/app/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('class="browser-dock browser-glass"', idx)
        self.assertNotIn('<aside class="browser-panel"', idx,
                         "浮动 <aside class=\"browser-panel\"> 已废弃")


if __name__ == "__main__":
    unittest.main()
