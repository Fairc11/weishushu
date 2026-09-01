"""前端静态拆分契约测试。

这些测试不启动浏览器，只检查模板加载顺序和 JS 文件的公开边界。
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app
from frontend_assets import (
    FRONTEND_MODULES,
    PRODUCTION_CSS,
    css_bundle_asset,
    frontend_bundle_asset,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_HTML = ROOT / "backend" / "app" / "templates" / "base.html"
STATIC_JS = ROOT / "backend" / "app" / "static" / "js"
APP_JS = frontend_bundle_asset()
API_CLIENT_JS = STATIC_JS / "api_client.js"
DESKTOP_BRIDGE_JS = STATIC_JS / "desktop_bridge.js"
STATIC_CSS = ROOT / "backend" / "app" / "static" / "css"
APP_CSS = css_bundle_asset()
APP_WIN_CSS = STATIC_CSS / "app-win.css"
APP_WIN_JS = STATIC_JS / "app-win.js"
INDEX_HTML = ROOT / "backend" / "app" / "templates" / "index.html"


def run_node(source: str) -> None:
    result = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"Node 契约失败:\n{result.stdout}\n{result.stderr}")


class FrontendStaticContractTests(unittest.TestCase):
    def test_base_loads_six_frontend_modules_in_order(self):
        source = BASE_HTML.read_text(encoding="utf-8")
        positions = []
        for name in FRONTEND_MODULES:
            marker = f'/static/js/modules/{name}?v={{{{ version }}}}'
            position = source.find(marker)
            self.assertGreater(position, -1, marker)
            positions.append(position)
            self.assertIn(
                f'<script type="module" src="{marker}"></script>',
                source,
            )
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('/static/js/app.js?', source)

    def test_exact_six_modules_exist_and_legacy_app_is_removed(self):
        module_root = STATIC_JS / "modules"
        for name in FRONTEND_MODULES:
            path = module_root / name
            self.assertTrue(path.is_file(), name)
            self.assertIn("export function init(", path.read_text(encoding="utf-8"))
        self.assertFalse((STATIC_JS / "app.js").exists())

    def test_frontend_module_import_boundaries_are_acyclic(self):
        module_root = STATIC_JS / "modules"
        sources = {
            name: (module_root / name).read_text(encoding="utf-8")
            for name in FRONTEND_MODULES
        }
        self.assertNotIn("import ", sources["state.js"])
        self.assertIn('from "./state.js"', sources["feedback.js"])
        self.assertNotIn('from "./feedback.js"', sources["state.js"])
        for name in ("login.js", "archive.js", "tasks.js"):
            self.assertIn('from "./state.js"', sources[name])
            self.assertIn('from "./feedback.js"', sources[name])
        self.assertIn('from "./state.js"', sources["desktop.js"])
        for forbidden in ("feedback.js", "login.js", "archive.js", "tasks.js"):
            self.assertNotIn(f'from "./{forbidden}"', sources["desktop.js"])

    def test_base_loads_six_production_css_files_in_order(self):
        src = BASE_HTML.read_text(encoding="utf-8")
        positions = []
        for name in PRODUCTION_CSS:
            marker = f'/static/css/{name}?v={{{{ version }}}}'
            position = src.find(marker)
            self.assertGreater(position, -1, marker)
            positions.append(position)
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('/static/css/app.css?', src)

    def test_release_url_input_has_no_account_identifier(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            'id="url-input" class="text-input" placeholder="输入博主昵称搜索，或粘贴主页链接 / 分享文本"',
            src,
        )

    def test_base_loads_bridge_and_api_before_modules(self):
        src = BASE_HTML.read_text(encoding="utf-8")
        scripts = [
            '/static/js/desktop_bridge.js?v={{ version }}',
            '/static/js/api_client.js?v={{ version }}',
            '/static/js/modules/state.js?v={{ version }}',
        ]
        positions = []
        for script in scripts:
            pos = src.find(script)
            self.assertGreater(pos, -1, f"{script} 未在 base.html 中加载")
            positions.append(pos)
        self.assertEqual(positions, sorted(positions), "前端基础脚本加载顺序不正确")

    def test_new_static_js_assets_are_served(self):
        client = TestClient(app)
        for path in (
            "/static/js/desktop_bridge.js",
            "/static/js/api_client.js",
            "/static/js/modules/state.js",
            "/static/js/modules/feedback.js",
            "/static/js/modules/login.js",
            "/static/js/modules/archive.js",
            "/static/js/modules/tasks.js",
            "/static/js/modules/desktop.js",
            "/static/js/ws_client.js",
        ):
            r = client.get(path)
            self.assertEqual(r.status_code, 200, f"{path} not served")
            self.assertGreater(len(r.text), 100, f"{path} too small")

    def test_api_client_exports_expected_http_methods(self):
        src = API_CLIENT_JS.read_text(encoding="utf-8")
        self.assertIn("global.WeishushuApi", src)
        for method in (
            "profileResolve",
            "preview",
            "start",
            "loginStatus",
            "loginQrcode",
            "loginChrome",
            "tailLogs",
            "whoami",
            "backupInspect",
            "backupStart",
            "followingStart",
            "backupList",
            "backupSearch",
            "firstRunCheck",
            "firstRunAccept",
        ):
            self.assertIn(f"{method}(", src)

    def test_desktop_bridge_exports_expected_methods(self):
        src = DESKTOP_BRIDGE_JS.read_text(encoding="utf-8")
        self.assertIn("global.WeishushuDesktopBridge", src)
        for method in (
            "isAvailable",
            "call",
            "openBrowserWindow",
            "copyUrlToMain",
            "getCopiedUrl",
            "refreshBrowser",
            "closeBrowserWindow",
            "getBrowserCurrentUrl",
            "browserBack",
            "browserForward",
            "injectCookies",
            "syncBrowserLogin",
        ):
            self.assertIn(f"{method}(", src)

    def test_app_uses_extracted_api_client(self):
        src = APP_JS.read_text(encoding="utf-8")
        self.assertIn("Api: global.WeishushuApi", src)
        self.assertNotIn("async _fetch(path", src)
        for bridge_method in (
            "open_browser_window",
            "copy_url_to_main",
            "get_copied_url",
            "refresh_browser",
            "close_browser_window",
            "get_browser_current_url",
            "browser_back",
            "browser_forward",
            "inject_cookies",
        ):
            self.assertNotIn(bridge_method, src)

    def test_login_button_uses_qrcode_modal_entry(self):
        html = (ROOT / "backend" / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        src = APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="btn-qrcode"', html)
        self.assertIn("扫码登录", html)
        self.assertIn('id="qrcode-login-overlay"', html)
        self.assertIn("Ptu.loginQrcode()", src)
        self.assertIn("pollQrcodeStatus()", src)

    def test_builtin_browser_entries_share_continuous_login_sync(self):
        src = APP_JS.read_text(encoding="utf-8")
        sync = re.search(
            r"async syncBuiltinBrowserLoginStatus\s*\(\)\s*\{(.*?)\n\s*\},",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(sync, "缺少统一的内置浏览器登录同步函数")
        body = sync.group(1)
        native_sync = body.find("Ptu.Api.syncBrowserLogin()")
        status_refresh = body.find("Ptu.refreshLoginStatus()")
        self.assertGreaterEqual(native_sync, 0, "统一登录同步没有导出浏览器 Cookie")
        self.assertGreater(
            status_refresh,
            native_sync,
            "必须先把浏览器 Cookie 写回当前配置，再刷新主界面登录状态",
        )

        watch = re.search(
            r"startLoginStatusWatch\s*\(\)\s*\{(.*?)\n\s*\},",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(watch, "缺少登录状态监视器")
        self.assertIn(
            "Ptu.syncBuiltinBrowserLoginStatus()",
            watch.group(1),
            "登录监视器仍然只检查文件，没有持续同步内置浏览器会话",
        )

        opened = re.search(
            r"async openBrowser\s*\(\)\s*\{(.*?)\n\s*\},",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(opened, "缺少内置浏览器入口")
        self.assertIn(
            "Ptu.startLoginStatusWatch()",
            opened.group(1),
            "右上角浏览器入口没有复用顶部登录入口的会话监视",
        )

        login = re.search(
            r"async loginWithBuiltinBrowser\s*\(\)\s*\{(.*?)\n\s*\},",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(login, "缺少顶部内置浏览器登录入口")
        self.assertNotIn(
            "Ptu.startLoginStatusWatch()",
            login.group(1),
            "顶部登录入口不应另建一套重复监视逻辑",
        )

    def test_preview_reveals_existing_settings_step(self):
        html = (ROOT / "backend" / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        src = APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="step-3"', html)
        self.assertNotIn('id="step-4"', html)
        self.assertIn("Ptu.$('step-3').hidden = false", src)
        self.assertNotIn("Ptu.$('step-4').hidden = false", src)

    def test_css_display_components_respect_hidden_attribute(self):
        css = APP_CSS.read_text(encoding="utf-8")
        for selector in (
            r"\.user-card\[hidden\]",
            r"\.backup-self-block\[hidden\]",
            r"\.history-panel\[hidden\]",
            r"\.log-panel\[hidden\]",
        ):
            self.assertRegex(
                css,
                selector + r"\s*\{[^}]*display\s*:\s*none\s*!important",
                f"{selector} 缺少 [hidden] display none 兜底",
            )

    def test_risk_modal_event_handlers_are_bound_once(self):
        src = APP_JS.read_text(encoding="utf-8")
        self.assertIn("riskModalBound: false", src)
        self.assertIn("if (Ptu.State.riskModalBound) return;", src)
        self.assertIn("Ptu.State.riskModalBound = true", src)

    def test_personal_archive_dialog_has_exact_actions_and_accessibility(self):
        src = APP_JS.read_text(encoding="utf-8")
        html = INDEX_HTML.read_text(encoding="utf-8")
        for token in (
            'id="archive-folder-decision"',
            'role="dialog"',
            'aria-modal="true"',
            'aria-labelledby="archive-folder-decision-title"',
            'data-archive-mode="incremental"',
            '新增备份（推荐）',
            'id="archive-action-following"',
            '更新关注资料',
            'data-archive-mode="rebuild"',
            '重新全部备份',
            '取消并重新选择',
            '在此创建微博书',
        ):
            self.assertIn(token, html)
        self.assertIn("backupFolderDir: null", src)
        self.assertIn("event.key === 'Escape'", src)
        self.assertIn("Ptu.State.backupFolderDir", src)
        self.assertIn("Ptu.createArchiveInSelectedFolder", src)
        self.assertIn("昵称_UID", src)

    def test_backup_api_sends_only_new_request_bodies(self):
        api_source = API_CLIENT_JS.read_text(encoding="utf-8")
        run_node(f"""
const calls = [];
global.window = {{}};
global.fetch = async (path, options) => ({{ ok: true, json: async () => ({{}}) }});
eval({api_source!r});
window.WeishushuApi._fetch = async (path, options) => {{
  calls.push([path, JSON.parse(options.body)]);
  return {{}};
}};
(async () => {{
  await window.WeishushuApi.backupInspect({{ output_dir: '/tmp/微博书' }});
  await window.WeishushuApi.backupStart({{ output_dir: '/tmp/微博书', mode: 'rebuild' }});
  const expected = [
    ['/api/backup/inspect', {{ output_dir: '/tmp/微博书' }}],
    ['/api/backup/start', {{ output_dir: '/tmp/微博书', mode: 'rebuild' }}],
  ];
  if (JSON.stringify(calls) !== JSON.stringify(expected)) throw new Error(JSON.stringify(calls));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
""")

    def test_backup_inspect_aborts_but_start_waits_for_a_definite_response(self):
        api_source = API_CLIENT_JS.read_text(encoding="utf-8")
        run_node(f"""
const timers = [];
let cleared = 0;
const calls = [];
let resolveStart;
global.window = {{}};
global.setTimeout = (callback, ms) => {{ timers.push([callback, ms]); return timers.length; }};
global.clearTimeout = () => {{ cleared += 1; }};
global.fetch = (path, options) => {{
  calls.push([path, options.signal]);
  if (path === '/api/backup/start') return new Promise((resolve) => {{ resolveStart = resolve; }});
  if (!options.signal) return Promise.resolve({{ ok:true, json:async () => ({{ ok:true }}) }});
  return new Promise((resolve, reject) => {{
    options.signal.addEventListener('abort', () => {{
      const error = new Error('aborted');
      error.name = 'AbortError';
      reject(error);
    }});
  }});
}};
eval({api_source!r});
(async () => {{
  const inspectPending = window.WeishushuApi.backupInspect({{ output_dir:'/tmp/book' }});
  await Promise.resolve();
  if (timers[0][1] !== 15000 || !calls[0][1]) throw new Error('inspect 未设置 15 秒 abort');
  timers[0][0]();
  try {{ await inspectPending; throw new Error('inspect 未抛出超时'); }}
  catch (error) {{ if (error.message !== '请求超时，请重试') throw error; }}
  if (cleared !== 1) throw new Error('inspect finally 未清理 timer');

  const timerCount = timers.length;
  let startSettled = false;
  const startPending = window.WeishushuApi.backupStart({{ output_dir:'/tmp/book', mode:'create' }}).then((value) => {{ startSettled=true; return value; }});
  await Promise.resolve();
  if (timers.length !== timerCount || calls[1][1] !== undefined) throw new Error('start 不应设置 abort timeout');
  if (startSettled) throw new Error('start 未等待确定响应');
  resolveStart({{ ok:true, json:async () => ({{ task_id:'task-1' }}) }});
  const started = await startPending;
  if (started.task_id !== 'task-1') throw new Error('start 响应丢失');

  await window.WeishushuApi.profileResolve('https://weibo.com/u/10001');
  if (timers.length !== timerCount || calls[2][1] !== undefined) throw new Error('inspect 超时设置影响了其他 API');
}})().catch((error) => {{ console.error(error); process.exit(1); }});
""")

    def test_old_personal_backup_override_fields_are_absent(self):
        src = APP_JS.read_text(encoding="utf-8")
        for token in ("confirm_uid_mismatch", "refresh_index"):
            self.assertNotIn(token, src)

    def test_start_blocks_empty_preview_selection_before_request(self):
        src = APP_JS.read_text(encoding="utf-8")
        match = re.search(r"async start\s*\(\)\s*\{(.*?)\n\s*\},", src, re.DOTALL)
        self.assertIsNotNone(match, "start 方法未找到")
        body = match.group(1)
        guard = body.find("if (!Ptu.State.selectedBids.size)")
        request = body.find("Ptu.Api.start(req)")
        self.assertGreaterEqual(guard, 0, "预览结果为空时没有禁止启动")
        self.assertGreater(request, guard, "空选择必须在发起请求前拦截")
        self.assertIn("请至少选择一条微博", body)
        self.assertIn("postIds: Ptu.State.selectedBids", body)
        self.assertIn("scopeRequest = Ptu.buildScopeRequest(scope, scopeValues)", body)
        helper = re.search(r"buildScopeRequest\s*\(scope, values\)\s*\{(.*?)\n\s*\},", src, re.DOTALL)
        self.assertIsNotNone(helper, "buildScopeRequest 方法未找到")
        self.assertIn("request.post_ids = postIds", helper.group(1))
        self.assertNotIn("post_ids: Ptu.State.selectedBids.size", body)

    def test_production_css_uses_tokens_instead_of_color_mix(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertNotIn("color-mix(", css)
        self.assertNotIn("--ios-", css)
        self.assertIn("var(--wb-brand-soft)", css)

    def test_production_css_keeps_650_weight_fallback(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("font-weight: 650", css)
        self.assertIn("@supports not (font-weight: 650)", css)

    def test_production_css_has_dvh_fallback(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("100dvh", css)

    def test_production_css_has_backdrop_filter_fallback(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("backdrop-filter", css)
        self.assertIn("@supports not (backdrop-filter: blur(1px))", css)

    def test_production_css_has_standard_scrollbar_properties(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("scrollbar-width: thin", css)
        self.assertIn("scrollbar-color:", css)

    def test_production_css_font_family_includes_segoe_ui(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn('"Segoe UI"', css)

    def test_app_js_short_circuits_non_darwin_sync_native_browser_frame(self):
        """v2.0.0 阶段 4：syncNativeBrowserFrame 必须在非 darwin 平台短路。"""
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("platform !== 'darwin'", js)

    def test_app_js_uses_inner_width_instead_of_match_media_for_split(self):
        """v2.0.0 阶段 4：applySplitRatio 用 innerWidth 替代 matchMedia。"""
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("global.innerWidth <= 1100", js)
        # matchMedia 不应再出现在 split 逻辑里（注释除外）
        for line in js.splitlines():
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            self.assertNotIn("matchMedia", stripped, f"app.js 仍使用 matchMedia: {stripped}")

    def test_win_assets_exist_and_loaded_conditionally(self):
        """v2.0.0 阶段 4：app-win.css / app-win.js 存在，且 base.html 按 platform 条件引入。"""
        self.assertTrue(APP_WIN_CSS.exists(), "app-win.css 不存在")
        self.assertTrue(APP_WIN_JS.exists(), "app-win.js 不存在")
        html = BASE_HTML.read_text(encoding="utf-8")
        self.assertIn("platform == 'win32'", html)
        self.assertIn("app-win.css?v={{ version }}", html)
        self.assertIn("app-win.js?v={{ version }}", html)

    def test_win_notice_removed(self):
        """v2.0.1：Windows 顶部「完整体验请使用 Mac 版」提示条已删除。"""
        html = BASE_HTML.read_text(encoding="utf-8")
        css = APP_WIN_CSS.read_text(encoding="utf-8")
        self.assertNotIn("win-notice", html)
        self.assertNotIn("win-notice", css)
        self.assertNotIn("完整体验请使用 Mac 版软件", html)

    def test_win_assets_served_via_static_mount(self):
        """v2.0.0 阶段 4：app-win.css / app-win.js 能通过 /static/ 路由访问。"""
        client = TestClient(app)
        for path in ("/static/css/app-win.css", "/static/js/app-win.js"):
            r = client.get(path)
            self.assertEqual(r.status_code, 200, f"{path} not served")
            self.assertGreater(len(r.text), 50, f"{path} too small")

    def test_progress_stepper_survives_archive_sync_extra_phases(self):
        """v2.0.1：discover/comments 等六步之外的阶段不得清空步骤条。"""
        tasks_source = (STATIC_JS / "modules" / "tasks.js").read_text(encoding="utf-8")
        run_node(f"""
const phases = ['identify', 'extract', 'media', 'generate', 'report', 'complete'];
const items = phases.map((phase) => {{
  const classes = new Set();
  return {{
    dataset: {{ progressPhase: phase }},
    classes,
    classList: {{ toggle: (name, force) => {{ force ? classes.add(name) : classes.delete(name); }} }},
  }};
}});
const stubNode = () => ({{
  textContent: '',
  style: {{}},
  hidden: false,
  classList: {{ toggle: () => {{}} }},
  querySelector: () => null,
}});
const nodes = {{}};
globalThis.document = {{
  querySelectorAll: (selector) => selector === '[data-progress-phase]' ? items : [],
}};
globalThis.Ptu = {{
  $: (id) => nodes[id] || (nodes[id] = stubNode()),
  State: {{}},
}};
const transformed = {tasks_source!r}
  .split('\\n')
  .filter((line) => !line.startsWith('import '))
  .join('\\n')
  .replace('export function init', 'function init')
  .replace('void initFeedback;', '');
eval(transformed);
const call = (phase) => Ptu.renderProgressEvent({{
  phase, pct: 0.1, detail: 'x', current: 1, total: 2, unit: 'post',
}});
call('identify');
if (!items[0].classes.has('is-active')) throw new Error('identify 未激活');
call('discover');
if (!items[0].classes.has('is-done')) throw new Error('discover 清空了已完成步骤');
if (!items[1].classes.has('is-active')) throw new Error('discover 未映射到抓取微博');
call('comments');
if (!items[0].classes.has('is-done') || !items[1].classes.has('is-active')) {{
  throw new Error('comments 阶段步骤条状态错误');
}}
call('media');
if (!items[2].classes.has('is-active') || !items[1].classes.has('is-done')) {{
  throw new Error('media 阶段步骤条状态错误');
}}
call('bloggers');
if (!items[2].classes.has('is-active')) throw new Error('未知阶段清空了步骤条');
call('complete');
if (!items.every((item) => item.classes.has('is-done'))) {{
  throw new Error('complete 未将全部步骤置为完成');
}}
if (items.some((item) => item.classes.has('is-active'))) {{
  throw new Error('complete 后仍有步骤处于 active');
}}
""")


if __name__ == "__main__":
    unittest.main()
