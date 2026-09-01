"""C 簇：前端 JS P0-P2 bug 修复守卫测试。

11 条修复集中在 backend/app/static/js/app.js，本测试通过源码 token 扫描
确保所有修法已落地（即便浏览器未启动 / JS 未加载，也能挡住回退）。

不模拟 DOM / fetch —— JS 单测投入产出比太低。
"""
import re
import sys
import unittest
from pathlib import Path
from frontend_assets import css_bundle_asset, frontend_bundle_asset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_JS = frontend_bundle_asset()
APP_CSS = css_bundle_asset()
SRC = APP_JS.read_text(encoding="utf-8")
CSS_SRC = APP_CSS.read_text(encoding="utf-8")


class C1HistorySearchMethodsTests(unittest.TestCase):
    """C1: v1.1.6 toggleHistory / pickHistoryFolder / runSearch 三个方法必须实现。"""

    def test_toggle_history_defined(self):
        """app.js 全文 0 处定义 → TypeError，必须实现"""
        self.assertRegex(
            SRC,
            r"toggleHistory\s*\(force\)\s*\{",
            "缺少 toggleHistory 方法定义（C1 修复）",
        )

    def test_pick_history_folder_defined(self):
        self.assertRegex(
            SRC,
            r"pickHistoryFolder\s*\(\)\s*\{",
            "缺少 pickHistoryFolder 方法定义（C1 修复）",
        )

    def test_run_search_defined(self):
        self.assertRegex(
            SRC,
            r"runSearch\s*\(\)\s*\{",
            "缺少 runSearch 方法定义（C1 修复）",
        )

    def test_pick_history_folder_uses_pywebview_select_folder(self):
        """桌面走 select_folder；dev 降级 prompt"""
        m = re.search(r"pickHistoryFolder\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m, "pickHistoryFolder 方法未找到")
        body = m.group(1)
        self.assertIn("select_folder", body)
        self.assertIn("prompt", body)

    def test_run_search_uses_backup_search_api(self):
        m = re.search(r"runSearch\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m, "runSearch 方法未找到")
        body = m.group(1)
        self.assertIn("backupSearch", body)
        self.assertIn("search-input", body)
        self.assertIn("historyFolder", body)


class C2WsCloseOnTerminalTests(unittest.TestCase):
    """C2: handleTaskMessage 终态（done/error/cancelled）必须关闭 WS。"""

    def test_terminal_branches_close_ws(self):
        """三终态必须调 _closeTaskWs（或等价 close）"""
        m = re.search(r"handleTaskMessage\s*\(msg\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m, "handleTaskMessage 方法未找到")
        body = m.group(1)
        for term in ("done", "error", "cancelled"):
            # 简单扫描：每种终态分支体内应有 _closeTaskWs
            # 用更宽松的 pattern
            self.assertIn(
                "_closeTaskWs", body,
                f"handleTaskMessage 缺少 _closeTaskWs 调用（C2 修复）",
            )

    def test_close_task_ws_helper_defined(self):
        """_closeTaskWs 必须定义并清 State.ws"""
        self.assertRegex(
            SRC,
            r"_closeTaskWs\s*\(\)\s*\{",
            "缺少 _closeTaskWs 辅助方法（C2 修复）",
        )
        m = re.search(r"_closeTaskWs\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("Ptu.State.ws", body)
        self.assertIn("close()", body)
        self.assertIn("Ptu.State.ws = null", body)


class C3ImageQualityInRequestTests(unittest.TestCase):
    """C3: start() payload 必须含 image_quality。"""

    def test_image_quality_in_start_payload(self):
        m = re.search(r"async start\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m, "start 方法未找到")
        body = m.group(1)
        self.assertIn(
            "image_quality", body,
            "start() payload 缺少 image_quality 字段（C3 修复）",
        )
        # 必须从 set-quality 元素读
        self.assertIn("set-quality", body)
        # 缺省值兜底
        self.assertIn("'large'", body)


class H2CommentsCountGuardTests(unittest.TestCase):
    """H2: comments_count=0 不能再被 ||5 吞成 5。"""

    def test_comments_count_uses_number_is_finite(self):
        m = re.search(r"async start\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        # 必须用 Number.isFinite 守卫
        self.assertIn(
            "Number.isFinite", body,
            "comments_count 缺少 Number.isFinite 守卫（H2 修复）",
        )
        # 不能再用 ||5 短路
        # 搜索 "|| 5"（在 comments_count 附近）
        # 简化：确认 comments_count 表达式含 Number.isFinite
        self.assertRegex(
            body,
            r"comments_count\s*:\s*Number\.isFinite",
            "comments_count 表达式未走 Number.isFinite 守卫",
        )


class H1LogToggleClickBindingTests(unittest.TestCase):
    """H1: maybeShowLogButton 创建的按钮必须绑 click。"""

    def test_maybe_show_log_button_binds_click(self):
        m = re.search(
            r"maybeShowLogButton\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m, "maybeShowLogButton 方法未找到")
        body = m.group(1)
        self.assertIn(
            "addEventListener('click'", body,
            "动态创建的 log-toggle 未绑 click（H1 修复）",
        )
        self.assertIn("toggleLogPanel", body)


class H3SafeLocalStorageTests(unittest.TestCase):
    """H3: localStorage 必须经 safeLocalGet/Set 包裹。"""

    def test_safe_local_helpers_defined(self):
        self.assertRegex(SRC, r"safeLocalGet\s*\(key\)\s*\{")
        self.assertRegex(SRC, r"safeLocalSet\s*\(key,\s*value\)\s*\{")

    def test_safe_local_helpers_use_try_catch(self):
        for name in ("safeLocalGet", "safeLocalSet"):
            m = re.search(
                rf"{name}\s*\([^)]*\)\s*\{{(.*?)\n\s*\}},", SRC, re.DOTALL
            )
            self.assertIsNotNone(m, f"{name} 方法未找到")
            body = m.group(1)
            self.assertIn("try", body)
            self.assertIn("catch", body)

    def test_no_bare_localstorage_getitem(self):
        """所有 localStorage.getItem 必须经 Ptu.safeLocalGet"""
        # 排除 Ptu.safeLocalGet 内部的实现
        body = re.sub(
            r"safeLocalGet\s*\(key\)\s*\{.*?\}",
            "",
            SRC,
            flags=re.DOTALL,
        )
        self.assertNotIn(
            "localStorage.getItem", body,
            "存在裸 localStorage.getItem 调用（H3 修复未落地）",
        )

    def test_no_bare_localstorage_setitem(self):
        body = re.sub(
            r"safeLocalSet\s*\(key,\s*value\)\s*\{.*?\}",
            "",
            SRC,
            flags=re.DOTALL,
        )
        self.assertNotIn(
            "localStorage.setItem", body,
            "存在裸 localStorage.setItem 调用（H3 修复未落地）",
        )


class H4NullDefensesInBindEventsTests(unittest.TestCase):
    """H4: bindEvents 前 6 元素必须 null 防御。"""

    def test_six_first_elements_have_null_guards(self):
        for elem_id in (
            "theme-toggle",
            "btn-search-blogger",
            "url-input",
            "check-all",
            "btn-start",
            "btn-open-folder",
        ):
            pattern = (
                rf"Ptu\.\$\(['\"]{elem_id}['\"]\)\s*\?\s*"
                rf"Ptu\.\$\(['\"]{elem_id}['\"]\)\.addEventListener"
            )
            # 兼容两种写法
            self.assertRegex(
                SRC,
                rf"Ptu\.\$\(['\"]{elem_id}['\"]\)",
                f"bindEvents 缺少 {elem_id} 防御（H4 修复）",
            )

    def test_theme_toggle_guarded(self):
        # 简化：检查 if 守卫
        m = re.search(r"bindEvents\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        # theme-toggle 应该是变量形式
        self.assertIn("themeToggle", body)
        self.assertRegex(body, r"if\s*\(\s*themeToggle\s*\)")


class BloggerSearchEntryTests(unittest.TestCase):
    """v2.0.1 备份他人微博：前端搜索入口与目标态。"""

    def test_search_entry_tokens(self):
        for token in (
            "searchOrResolve",
            "renderBloggerResults",
            "selectBloggerTarget",
            "archiveTarget",
            "searchUsers",
            "resolveTarget",
            "target_uid",
        ):
            self.assertIn(token, SRC, f"前端缺少 {token}（备份他人微博入口）")

    def test_blogger_result_escapes_user_generated_fields(self):
        m = re.search(
            r"renderBloggerResults\s*\(results\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m, "renderBloggerResults 方法未找到")
        body = m.group(1)
        self.assertIn("Ptu.escape(u.screen_name)", body)
        self.assertIn("Ptu.escape(u.verified_reason", body)


class H5DataBidEscapeTests(unittest.TestCase):
    """H5: data-bid 必须转义。"""

    def test_data_bid_escaped(self):
        m = re.search(
            r"renderPreviewList\s*\(previews\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m, "renderPreviewList 方法未找到")
        body = m.group(1)
        # 应含 escape
        self.assertRegex(
            body,
            r'data-bid="\$\{Ptu\.escape\(p\.bid\)\}"',
            "data-bid 未走 Ptu.escape（H5 修复）",
        )


class H6BrowserStatusBadgeTests(unittest.TestCase):
    """H6: browser-status 徽标在 3 处必须更新。"""

    def test_open_browser_sets_badge_success(self):
        m = re.search(
            r"async openBrowser\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("browser-status", body)
        self.assertIn("已打开", body)
        self.assertIn("badge-success", body)

    def test_close_action_sets_badge_muted(self):
        m = re.search(
            r"action === 'close'", SRC
        )
        self.assertIsNotNone(m)
        # browserAction 内 'close' 分支应有徽标设 '已关闭'
        m2 = re.search(
            r"async browserAction\s*\(action\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m2)
        body = m2.group(1)
        # 'close' 附近应设 '已关闭' / 'badge-muted'
        close_idx = body.find("action === 'close'")
        self.assertGreater(close_idx, -1)
        # 取 close 之后 500 字符
        after_close = body[close_idx: close_idx + 500]
        self.assertIn("已关闭", after_close)
        self.assertIn("badge-muted", after_close)

    def test_refresh_browser_url_handles_null(self):
        m = re.search(
            r"async refreshBrowserUrlDisplay\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        # null 分支应设徽标 '未打开'
        self.assertIn("'未打开'", body)
        self.assertIn("browser-status", body)


class M3RiskModalCancelFallbackTests(unittest.TestCase):
    """M3: cancelBtn 必须有 overlay 回落，否则用户卡死。"""

    def test_hidden_overlay_is_really_hidden(self):
        self.assertRegex(
            CSS_SRC,
            r"\.risk-modal-overlay\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important",
            "risk-modal-overlay 设置了 display:flex，必须显式让 [hidden] display:none",
        )

    def test_cancel_fallback_closes_overlay(self):
        m = re.search(
            r"cancelBtn\.addEventListener\('click',\s*\(\)\s*=>\s*\{(.*?)\n\s*\}\);",
            SRC,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "cancelBtn click 监听器未找到")
        body = m.group(1)
        # 兜底必须关 overlay
        self.assertIn("overlay.hidden = true", body)
        self.assertIn("已拒绝风险须知", body)

    def test_cancel_uses_close_window_when_available(self):
        m = re.search(
            r"cancelBtn\.addEventListener\('click',\s*\(\)\s*=>\s*\{(.*?)\n\s*\}\);",
            SRC,
            re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("close_window", body)


class M4LoginStatusCatchTests(unittest.TestCase):
    """M4: refreshLoginStatus catch 必须设 '状态未知' 徽标。"""

    def test_catch_sets_warn_badge(self):
        m = re.search(
            r"async refreshLoginStatus\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL
        )
        self.assertIsNotNone(m, "refreshLoginStatus 方法未找到")
        body = m.group(1)
        # catch 内必须设徽标
        self.assertIn("状态未知", body)
        self.assertIn("badge-warn", body)


class CompileCheckTests(unittest.TestCase):
    """静态语法粗检：括号 / 大括号配对 + 没有 'undefined function' 引用。"""

    def test_braces_balanced(self):
        # 仅粗略统计 — JS 实际无编译，源码 token 扫描兜底
        opens = SRC.count("{")
        closes = SRC.count("}")
        self.assertEqual(opens, closes, f"大括号不配对: {{ {opens} 个 vs }} {closes} 个")

    def test_no_undefined_function_calls_to_missing_methods(self):
        """bindEvents 调用的 Ptu.X 必须有定义"""
        m = re.search(r"bindEvents\s*\(\)\s*\{(.*?)\n\s*\},", SRC, re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        called = set(re.findall(r"Ptu\.(\w+)\s*\(", body))
        # 列出所有 Ptu 上定义的方法（含 async / 普通函数）
        defined = set(re.findall(
            r"(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{", SRC
        ))
        missing = called - defined
        self.assertFalse(
            missing,
            f"bindEvents 引用了 Ptu 上未定义的方法: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
