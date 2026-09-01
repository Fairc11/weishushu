"""首页专注工作流的模板、交互和样式契约。"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from backend.app.main import app
from frontend_assets import css_bundle_asset, frontend_bundle_asset


ROOT = Path(__file__).resolve().parents[1]
APP_JS = frontend_bundle_asset()
APP_CSS = css_bundle_asset()
API_CLIENT_JS = ROOT / "backend" / "app" / "static" / "js" / "api_client.js"


def run_node(source: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".js", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            ["node", str(script_path)], cwd=ROOT, text=True, capture_output=True, check=False
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode:
        raise AssertionError(f"Node 状态机失败:\n{result.stdout}\n{result.stderr}")


def app_harness(test_source: str) -> str:
    app_source = APP_JS.read_text(encoding="utf-8")
    return f"""
class Element {{
  constructor(id) {{ this.id=id; this.hidden=false; this.disabled=false; this.textContent=''; this.dataset={{}}; this.attributes={{}}; this.focused=false; this.children=[]; this.style={{}}; }}
  addEventListener() {{}}
  setAttribute(name, value) {{ this.attributes[name]=String(value); }}
  getAttribute(name) {{ return this.attributes[name]; }}
  focus() {{ if (this.disabled) return; this.focused=true; document.activeElement=this; }}
  querySelector() {{ return null; }}
  querySelectorAll() {{ return this.children.filter((item) => !item.hidden); }}
}}
const elements = {{}};
const element = (id) => elements[id] || (elements[id] = new Element(id));
global.document = {{
  addEventListener() {{}},
  getElementById: element,
  querySelectorAll(selector) {{
    if (selector === '[data-archive-mode]') return [element('archive-action-incremental'), element('archive-action-rebuild')];
    return [];
  }},
  querySelector() {{ return null; }},
  body: new Element('body'),
}};
global.window = {{
  WeishushuApi: {{}},
  addEventListener() {{}},
  matchMedia() {{ return {{ matches: false }}; }},
  requestAnimationFrame(callback) {{ callback(); }},
}};
window.window = window;
global.requestAnimationFrame = window.requestAnimationFrame;
global.location = {{ protocol: 'http:', host: '127.0.0.1' }};
global.localStorage = {{ getItem() {{ return null; }}, setItem() {{}} }};
global.confirm = () => true;
global.prompt = () => '';
eval({app_source!r});
const Ptu = window.Ptu;
{test_source}
"""


class FocusedWorkflowRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_header_contains_login_disclosure_and_named_tools(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for token in (
            'id="login-menu-toggle"',
            'id="login-menu"',
            'class="login-menu" hidden',
            'aria-expanded="false"',
            'id="history-toggle"',
            'aria-label="历史记录"',
            'id="btn-browser"',
            'aria-label="浏览器"',
            'id="theme-toggle"',
        ):
            self.assertIn(token, response.text)

    def test_primary_url_action_precedes_settings_without_floating_history_button(self):
        response = self.client.get("/")
        html = response.text
        self.assertLess(html.index('id="btn-search-blogger"'), html.index('id="step-3"'))
        self.assertNotIn('class="history-toggle"', html)

    def test_blogger_search_entry_tokens(self):
        """v2.0.1 备份他人微博：首屏为搜索/链接双入口 + 结果列表容器。"""
        response = self.client.get("/")
        html = response.text
        for token in (
            'id="url-input"',
            'id="btn-search-blogger"',
            'id="blogger-results"',
            'id="archive-folder-eyebrow"',
        ):
            self.assertIn(token, html)

    def test_preview_and_settings_use_standard_content_groups(self):
        response = self.client.get("/")
        self.assertIn('class="preview-list"', response.text)
        self.assertIn('class="settings-grid settings-grouped"', response.text)

    def test_personal_backup_exposes_folder_decision_not_old_scope_choices(self):
        response = self.client.get("/")
        html = response.text
        for token in (
            'id="set-scope"',
            'id="archive-folder-decision"',
            'id="archive-folder-account"',
            'id="archive-folder-total"',
            'id="archive-folder-last-sync"',
            'id="archive-folder-path"',
        ):
            self.assertIn(token, html)

    def test_progress_section_contains_six_stage_card(self):
        response = self.client.get("/")
        html = response.text
        self.assertIn('id="progress-stage-card"', html)
        for phase in ("identify", "extract", "media", "generate", "report", "complete"):
            self.assertIn(f'data-progress-phase="{phase}"', html)
        for token in ('id="progress-stage-detail"', 'id="progress-stage-count"', 'id="progress-stage-elapsed"'):
            self.assertIn(token, html)

    def test_qrcode_login_modal_is_present_and_initially_hidden(self):
        response = self.client.get("/")
        html = response.text
        for token in (
            'id="qrcode-login-overlay" hidden',
            'id="qrcode-login-message"',
            'id="qrcode-login-image"',
            'id="qrcode-login-remaining"',
            'id="qrcode-login-retry"',
            'id="qrcode-login-cancel"',
            '扫码登录',
        ):
            self.assertIn(token, html)


class FocusedWorkflowJsTests(unittest.TestCase):
    def test_login_menu_state_and_aria_updates_are_defined(self):
        source = APP_JS.read_text(encoding="utf-8")
        for token in (
            "loginMenuOpen: false",
            "toggleLoginMenu(force)",
            "Ptu.$('login-menu-toggle')",
            "Ptu.$('login-menu')",
            "aria-expanded",
        ):
            self.assertIn(token, source)

    def test_future_entries_never_call_disabled_api_or_browser_bridge(self):
        run_node(app_harness("""
const calls = [];
const messages = [];
Ptu.Api.preview = async () => { calls.push('preview'); };
Ptu.Api.start = async () => { calls.push('start'); };
Ptu.Api.loginChrome = async () => { calls.push('chrome'); };
Ptu.Api.openBrowserWindow = async () => { calls.push('browser'); };
Ptu.toast = (message) => messages.push(message);
(async () => {
  await Ptu.preview();
  await Ptu.start();
  await Ptu.loginChrome();
  await Ptu.openBrowser();
  if (calls.length) throw new Error(`禁用入口仍发出请求: ${calls}`);
  if (messages.length !== 4 || messages.some((message) => message !== '该功能正在开发中。')) {
    throw new Error(JSON.stringify(messages));
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_qrcode_authentication_closes_modal_and_refreshes_login(self):
        run_node(app_harness("""
global.URL = {
  createObjectURL() { return 'blob:qrcode'; },
  revokeObjectURL() {},
};
global.setInterval = () => 11;
global.clearInterval = () => {};
const statuses = [
  { state:'waiting_scan', message:'请使用微博 App 扫描二维码', remaining_seconds:119, image_ready:true, result:null },
  { state:'authenticated', message:'登录成功', remaining_seconds:0, image_ready:false, result:{logged_in:true} },
];
let refreshed = 0;
Ptu.Api.loginQrcode = async () => ({ task_id:'task-login', session_id:'12345678-1234-4234-9234-1234567890ab' });
Ptu.Api.qrcodeStatus = async () => statuses.shift();
Ptu.Api.qrcodeImage = async () => ({});
Ptu.refreshLoginStatus = async () => { refreshed += 1; };
Ptu.toast = () => {};
(async () => {
  await Ptu.loginQrcode();
  if (element('qrcode-login-overlay').hidden) throw new Error('二维码模态框未显示');
  if (element('qrcode-login-image').src !== 'blob:qrcode') throw new Error('二维码图片未显示');
  await Ptu.pollQrcodeStatus();
  if (!element('qrcode-login-overlay').hidden) throw new Error('登录成功后未自动关闭');
  if (Ptu.State.qrcodeSessionId !== null || refreshed !== 1) throw new Error('登录成功后状态未刷新');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_expired_qrcode_stays_open_and_allows_refresh(self):
        run_node(app_harness("""
global.clearInterval = () => {};
Ptu.State.qrcodeSessionId = '12345678-1234-4234-9234-1234567890ab';
Ptu.Api.qrcodeStatus = async () => ({
  state:'expired', message:'二维码已过期，请重新获取', remaining_seconds:0,
  image_ready:false, result:null,
});
(async () => {
  await Ptu.pollQrcodeStatus();
  if (element('qrcode-login-overlay').hidden) throw new Error('过期后不应自动关闭');
  if (element('qrcode-login-retry').hidden) throw new Error('过期后未显示重新获取');
  if (!Ptu.State.qrcodeTerminal) throw new Error('过期状态未锁定');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_transient_qrcode_poll_error_keeps_session_cancellable(self):
        run_node(app_harness("""
Ptu.State.qrcodeSessionId = '12345678-1234-4234-9234-1234567890ab';
element('qrcode-login-retry').hidden = true;
Ptu.Api.qrcodeStatus = async () => { throw new Error('临时断线'); };
let cancelCalls = 0;
Ptu.Api.qrcodeCancel = async () => { cancelCalls += 1; return {cancelled:true,closed:true}; };
Ptu.toast = () => {};
(async () => {
  await Ptu.pollQrcodeStatus();
  if (Ptu.State.qrcodeTerminal) throw new Error('临时轮询失败被误判为终态');
  if (Ptu.State.qrcodeSessionId === null) throw new Error('临时轮询失败丢失会话');
  if (!element('qrcode-login-retry').hidden) throw new Error('临时轮询失败显示了新会话重试');
  await Ptu.cancelQrcodeLogin();
  if (cancelCalls !== 1) throw new Error('临时轮询失败后不能取消旧会话');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_cancel_during_qrcode_creation_cancels_late_session(self):
        run_node(app_harness("""
let resolveCreate;
Ptu.Api.loginQrcode = () => new Promise((resolve) => { resolveCreate = resolve; });
const cancelled = [];
Ptu.Api.qrcodeCancel = async (sessionId) => { cancelled.push(sessionId); return {cancelled:true,closed:true}; };
let statusCalls = 0;
Ptu.Api.qrcodeStatus = async () => { statusCalls += 1; return {}; };
Ptu.toast = () => {};
(async () => {
  const pending = Ptu.loginQrcode();
  await Promise.resolve();
  await Ptu.cancelQrcodeLogin();
  resolveCreate({ task_id:'late-task', session_id:'12345678-1234-4234-9234-1234567890ab' });
  await pending;
  if (JSON.stringify(cancelled) !== JSON.stringify(['12345678-1234-4234-9234-1234567890ab'])) throw new Error(JSON.stringify(cancelled));
  if (statusCalls !== 0) throw new Error('取消后的迟到会话仍开始轮询');
  if (Ptu.State.qrcodeSessionId !== null || !element('qrcode-login-overlay').hidden) throw new Error('取消后的迟到会话污染界面');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_qrcode_creation_ignores_double_click(self):
        run_node(app_harness("""
let createCalls = 0;
let resolveCreate;
Ptu.Api.loginQrcode = () => {
  createCalls += 1;
  return new Promise((resolve) => { resolveCreate = resolve; });
};
Ptu.Api.qrcodeStatus = async () => ({state:'waiting_scan',message:'等待扫码',remaining_seconds:119,image_ready:false,result:null});
global.setInterval = () => 1;
Ptu.toast = () => {};
(async () => {
  const first = Ptu.loginQrcode();
  const second = Ptu.loginQrcode();
  await Promise.resolve();
  if (createCalls !== 1) throw new Error(`重复创建 ${createCalls} 个会话`);
  resolveCreate({task_id:'task-1',session_id:'12345678-1234-4234-9234-1234567890ab'});
  await Promise.all([first, second]);
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_qrcode_client_has_status_image_cancel_and_unload_cleanup(self):
        api_source = API_CLIENT_JS.read_text(encoding="utf-8")
        app_source = APP_JS.read_text(encoding="utf-8")
        for token in (
            "qrcodeStatus(sessionId)",
            "qrcodeImage(sessionId)",
            "qrcodeCancel(sessionId",
            "Cache-Control",
        ):
            self.assertIn(token, api_source)
        for token in (
            "qrcodeSessionId: null",
            "pollQrcodeStatus()",
            "URL.revokeObjectURL",
            "beforeunload",
            "keepalive: true",
        ):
            self.assertIn(token, app_source)

    def test_mac_browser_auto_open_is_disconnected(self):
        source = APP_JS.read_text(encoding="utf-8")
        for token in (
            "firstRunAccepted: false",
            "maybeOpenMacBrowser()",
            "return false;",
            "global.addEventListener('pywebviewready'",
        ):
            self.assertIn(token, source)
        self.assertNotIn("Ptu.maybeOpenMacBrowser();", source)

    def test_main_scope_mapping_remains_available(self):
        source = APP_JS.read_text(encoding="utf-8")
        for token in (
            "buildScopeRequest(scope, values)",
            "Ptu.buildScopeRequest(scope, scopeValues)",
            "请至少选择一条微博",
            "总量尚未知",
        ):
            self.assertIn(token, source)

    def test_structured_progress_event_renders_real_counts(self):
        source = APP_JS.read_text(encoding="utf-8")
        for token in (
            "renderProgressEvent(event)",
            "event.total == null",
            "progress-stage-count",
            "progress-stage-elapsed",
            "msg.event",
        ):
            self.assertIn(token, source)

    def test_backup_selects_folder_then_inspects_and_guards_double_click(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("global.pywebview.api.select_folder('')", source)
        self.assertIn("await Ptu.Api.backupInspect(inspectReq)", source)
        run_node(app_harness("""
const button = element('btn-backup-self');
button.textContent = '一键备份';
let selectCalls = 0;
let resolveSelection;
window.pywebview = { api: { select_folder() { selectCalls += 1; return new Promise((resolve) => { resolveSelection = resolve; }); } } };
Ptu.State.isDesktop = true;
let inspectCalls = 0;
Ptu.Api.backupInspect = async () => { inspectCalls += 1; return { state: 'archive', path: '/tmp/book' }; };
Ptu.showArchiveFolderDecision = () => {};
(async () => {
  const first = Ptu.backupSelf();
  const second = Ptu.backupSelf();
  if (!button.disabled || button.textContent !== '正在检查文件夹…') throw new Error('loading 状态不正确');
  if (selectCalls !== 1) throw new Error('重复打开目录面板');
  resolveSelection(null);
  await Promise.all([first, second]);
  if (inspectCalls !== 0) throw new Error('取消后不应检查');
  if (button.disabled || button.textContent !== '一键备份') throw new Error('finally 未恢复');
  if (!button.focused) throw new Error('取消后未恢复一键备份焦点');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_folder_states_render_exact_safe_actions_and_messages(self):
        run_node(app_harness("""
const messages = [];
Ptu.toast = (message) => messages.push(message);
const overlay = element('archive-folder-decision');
for (const id of ['archive-action-create','archive-action-incremental','archive-action-rebuild','archive-action-create-subfolder','archive-action-reselect']) element(id);
Ptu.showArchiveFolderDecision({ state:'ordinary_nonempty', path:'/tmp/full' }, element('btn-backup-self'));
if (!element('archive-action-rebuild').hidden) throw new Error('ordinary 状态出现 rebuild');
if (element('archive-action-create-subfolder').hidden || element('archive-action-reselect').hidden) throw new Error('ordinary 动作不完整');
Ptu.handleArchiveInspection({ state:'uid_mismatch', path:'/tmp/a' }, element('btn-backup-self'));
Ptu.handleArchiveInspection({ state:'damaged', path:'/tmp/b' }, element('btn-backup-self'));
Ptu.handleArchiveInspection({ state:'legacy_index', path:'/tmp/c' }, element('btn-backup-self'));
const expected = ['该微博书属于其他登录账号','微博书档案不完整，请先复制目录后再修复'];
if (JSON.stringify(messages) !== JSON.stringify(expected)) throw new Error(JSON.stringify(messages));
if (overlay.hidden) throw new Error('legacy_index 未显示首次建档确认');
if (Ptu.State.backupFolderInspection.state !== 'legacy_index' || Ptu.State.backupFolderDir !== '/tmp/c') throw new Error('legacy_index 状态未保存');
if (element('archive-action-create').hidden) throw new Error('legacy_index 未显示 create');
if (!element('archive-action-incremental').hidden || !element('archive-action-rebuild').hidden) throw new Error('legacy_index 出现增量或重建');
if (element('archive-action-create').textContent !== '建立完整档案') throw new Error('legacy_index create 文案不正确');
if (element('archive-folder-warning').textContent !== '旧版备份目录，需要首次建立完整档案') throw new Error('legacy_index 提示不正确');
"""))

    def test_archive_modes_use_saved_path_and_reuse_task_progress(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("target ? `备份博主` : '本人模式'", source)
        self.assertIn("result.output_dir || Ptu.State._backupOutputDir", source)
        self.assertIn("result.generated_files", source)
        run_node(app_harness("""
Ptu.State.backupFolderDir = '/tmp/微博书';
Ptu.State.backupFolderInspection = { state:'archive', screen_name:'本人' };
let request = null;
let subscribed = null;
Ptu.Api.backupStart = async (body) => { request = body; return { task_id:'task-1', mode:body.mode, self_screen_name:'本人' }; };
Ptu.subscribeTask = (taskId) => { subscribed = taskId; };
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.closeArchiveFolderDecision = () => {};
Ptu.toast = () => {};
(async () => {
  await Ptu.startArchiveBackup('rebuild');
  const expected = {
    output_dir:'/tmp/微博书', mode:'rebuild',
    pacing_mode:'standard', keep_awake_when_plugged:false,
  };
  if (JSON.stringify(request) !== JSON.stringify(expected)) throw new Error(JSON.stringify(request));
  if (Ptu.State.taskId !== 'task-1' || subscribed !== 'task-1') throw new Error('未复用任务进度');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_legacy_confirmation_starts_create_not_incremental(self):
        run_node(app_harness("""
let request = null;
Ptu.Api.backupStart = async (body) => {
  request = body;
  return { task_id:'legacy-task', mode:body.mode, self_screen_name:'本人' };
};
Ptu.subscribeTask = () => {};
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.toast = () => {};
(async () => {
  await Ptu.handleArchiveInspection(
    { state:'legacy_index', path:'/tmp/legacy' },
    element('btn-backup-self'),
  );
  await Ptu.startArchiveBackup('create');
  const expected = {
    output_dir:'/tmp/legacy', mode:'create',
    pacing_mode:'standard', keep_awake_when_plugged:false,
  };
  if (JSON.stringify(request) !== JSON.stringify(expected)) throw new Error(JSON.stringify(request));
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_archive_dialog_moves_and_restores_focus_on_escape(self):
        run_node(app_harness("""
const trigger = element('btn-backup-self');
const first = element('archive-action-incremental');
Ptu.showArchiveFolderDecision({ state:'archive', path:'/tmp/book', screen_name:'本人', total_posts:3 }, trigger);
if (!first.focused) throw new Error('焦点未进入对话框');
let prevented = false;
Ptu.handleArchiveDialogKeydown({ key:'Escape', preventDefault() { prevented = true; } });
if (!prevented || !element('archive-folder-decision').hidden) throw new Error('Escape 未关闭');
if (!trigger.focused) throw new Error('关闭后未恢复焦点');
"""))

    def test_archive_dialog_cannot_close_while_start_request_is_pending(self):
        run_node(app_harness("""
Ptu.State.backupFolderDir = '/tmp/book';
const overlay = element('archive-folder-decision');
overlay.hidden = false;
let resolveStart;
Ptu.Api.backupStart = () => new Promise((resolve) => { resolveStart = resolve; });
Ptu.subscribeTask = () => {};
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.toast = () => {};
(async () => {
  const pending = Ptu.startArchiveBackup('incremental');
  if (!element('archive-action-incremental').disabled || !element('archive-action-reselect').disabled) throw new Error('启动期间动作未禁用');
  Ptu.handleArchiveDialogKeydown({ key:'Escape', preventDefault() {} });
  if (overlay.hidden) throw new Error('启动期间 Escape 关闭了对话框');
  Ptu.closeArchiveFolderDecision();
  if (overlay.hidden) throw new Error('启动期间遮罩关闭了对话框');
  resolveStart({ task_id:'task-1', self_screen_name:'本人' });
  await pending;
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_active_task_blocks_both_start_entries_without_touching_ws(self):
        run_node(app_harness("""
Ptu.State.taskId = 'active-task';
Ptu.State.backupFolderDir = '/tmp/book';
let selectCalls = 0;
let backupStartCalls = 0;
let ordinaryStartCalls = 0;
let wsCloseCalls = 0;
const messages = [];
window.pywebview = { api: { select_folder: async () => { selectCalls += 1; return '/tmp/book'; } } };
Ptu.Api.backupStart = async () => { backupStartCalls += 1; return {}; };
Ptu.Api.start = async () => { ordinaryStartCalls += 1; return {}; };
Ptu.State.ws = { close() { wsCloseCalls += 1; } };
Ptu.toast = (message) => messages.push(message);
(async () => {
  await Ptu.backupSelf();
  await Ptu.startArchiveBackup('incremental');
  await Ptu.start();
  if (selectCalls || backupStartCalls || ordinaryStartCalls) throw new Error('active task 仍发起了请求');
  if (wsCloseCalls || Ptu.State.taskId !== 'active-task') throw new Error('active guard 改动了当前任务');
  const expected = ['已有任务正在运行，请先等待完成或取消','已有任务正在运行，请先等待完成或取消','该功能正在开发中。'];
  if (JSON.stringify(messages) !== JSON.stringify(expected)) throw new Error(JSON.stringify(messages));
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_pending_ordinary_start_owns_shared_task_lock(self):
        run_node(app_harness("""
let ordinaryCalls = 0;
Ptu.Api.start = async () => { ordinaryCalls += 1; return {}; };
Ptu.toast = () => {};
(async () => {
  await Ptu.start();
  await Ptu.start();
  if (ordinaryCalls !== 0) throw new Error('禁用入口仍启动普通提取');
  if (Ptu.State.taskId !== null || Ptu.State.taskStartBusy) throw new Error('禁用入口污染任务状态');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_task_terminals_restore_both_start_buttons(self):
        run_node(app_harness("""
const backupButton = element('btn-backup-self');
const startButton = element('btn-start');
Ptu.setProgress = () => {};
Ptu.renderResult = () => {};
Ptu.toast = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu._closeTaskWs = () => {};
for (const type of ['done', 'error', 'cancelled']) {
  Ptu.State.taskId = `task-${type}`;
  Ptu.updateTaskActionAvailability();
  if (!backupButton.disabled || !startButton.disabled) throw new Error(`${type} 前按钮未禁用`);
  Ptu.handleTaskMessage({ type, result:{}, error:'错误' });
  if (Ptu.State.taskId !== null || backupButton.disabled || startButton.disabled) throw new Error(`${type} 未恢复按钮`);
}
"""))

    def test_cancel_request_keeps_active_lock_until_terminal_message(self):
        run_node(app_harness("""
Ptu.State.taskId = 'task-1';
Ptu.updateTaskActionAvailability();
Ptu.Api.cancelTask = async () => ({ cancelled:true });
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.toast = () => {};
(async () => {
  await Ptu.cancelTask();
  if (Ptu.State.taskId !== 'task-1') throw new Error('取消请求提前清理 taskId');
  if (!element('btn-start').disabled || !element('btn-backup-self').disabled) throw new Error('取消请求后提前解锁');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_backup_inspect_timeout_restores_button_aria_and_focus(self):
        run_node(app_harness("""
const button = element('btn-backup-self');
button.textContent = '一键备份';
window.pywebview = { api: { select_folder: async () => '/tmp/book' } };
Ptu.Api.backupInspect = async () => { throw new Error('请求超时，请重试'); };
const messages = [];
Ptu.toast = (message) => messages.push(message);
(async () => {
  await Ptu.backupSelf();
  if (button.disabled || button.getAttribute('aria-busy') !== 'false' || button.textContent !== '一键备份') throw new Error('检查超时未恢复按钮');
  if (!button.focused) throw new Error('检查超时未恢复焦点');
  if (messages[0] !== '目录检查失败：请求超时，请重试') throw new Error(JSON.stringify(messages));
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_backup_start_slow_notice_keeps_lock_until_definite_response(self):
        run_node(app_harness("""
Ptu.State.backupFolderDir = '/tmp/book';
const overlay = element('archive-folder-decision');
overlay.hidden = false;
let resolveStart;
let startCalls = 0;
let subscribed = null;
let slowTimer = null;
let slowTimerCleared = false;
const messages = [];
global.setTimeout = (callback, ms) => { if (ms === 15000) slowTimer = callback; return 1; };
global.clearTimeout = () => { slowTimerCleared = true; };
Ptu.Api.backupStart = () => { startCalls += 1; return new Promise((resolve) => { resolveStart = resolve; }); };
Ptu.subscribeTask = (taskId) => { subscribed = taskId; };
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.toast = (message) => messages.push(message);
(async () => {
  const pending = Ptu.startArchiveBackup('incremental');
  Ptu.startArchiveBackup('incremental');
  if (!slowTimer) throw new Error('未建立 15 秒慢启动提示');
  slowTimer();
  await Promise.resolve();
  if (!messages.includes('仍在启动，请勿重复操作')) throw new Error(JSON.stringify(messages));
  if (startCalls !== 1 || !Ptu.State.taskStartBusy || !Ptu.State.backupStartBusy) throw new Error('慢启动提示释放了锁');
  if (!element('btn-start').disabled || !element('btn-backup-self').disabled || !element('archive-action-incremental').disabled) throw new Error('慢启动提示恢复了按钮');
  if (overlay.hidden) throw new Error('慢启动提示关闭了 modal');
  resolveStart({ task_id:'task-1', self_screen_name:'本人' });
  await pending;
  if (Ptu.State.taskId !== 'task-1' || subscribed !== 'task-1' || !slowTimerCleared) throw new Error('确定响应后未继续任务或未清理提示计时器');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_backup_start_definite_http_error_unlocks_modal(self):
        run_node(app_harness("""
Ptu.State.backupFolderDir = '/tmp/book';
const overlay = element('archive-folder-decision');
overlay.hidden = false;
Ptu.Api.backupStart = async () => { throw new Error('409 目录不匹配'); };
Ptu.toast = () => {};
(async () => {
  await Ptu.startArchiveBackup('incremental');
  if (Ptu.State.taskStartBusy || Ptu.State.backupStartBusy || element('archive-action-incremental').disabled) throw new Error('确定 HTTP 错误后未解锁');
  Ptu.closeArchiveFolderDecision();
  if (!overlay.hidden) throw new Error('确定 HTTP 错误后无法关闭');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_reselect_cancel_restores_original_trigger_focus(self):
        run_node(app_harness("""
const trigger = element('btn-backup-self');
Ptu.State.backupFolderReturnFocus = trigger;
Ptu.State.backupFolderDir = '/tmp/book';
window.pywebview = { api: { select_folder: async () => null } };
(async () => {
  await Ptu.reselectArchiveFolder();
  if (!trigger.focused) throw new Error('重新选择取消后未恢复焦点');
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_terminal_inspection_states_restore_original_trigger_focus(self):
        run_node(app_harness("""
const trigger = element('btn-backup-self');
Ptu.toast = () => {};
(async () => {
  for (const state of ['uid_mismatch', 'damaged']) {
    trigger.focused = false;
    await Ptu.handleArchiveInspection({ state, path:'/tmp/book' }, trigger);
    if (!trigger.focused) throw new Error(`${state} 未恢复焦点`);
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_create_subfolder_action_starts_create_in_selected_folder(self):
        run_node(app_harness("""
const trigger = element('btn-backup-self');
Ptu.State.backupFolderReturnFocus = trigger;
Ptu.State.backupFolderDir = '/tmp/parent';
let request = null;
Ptu.Api.backupStart = async (body) => { request = body; return { task_id:'task-1', mode:body.mode, self_screen_name:'本人' }; };
Ptu.subscribeTask = () => {};
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.closeArchiveFolderDecision = () => {};
Ptu.toast = () => {};
(async () => {
  await Ptu.createArchiveInSelectedFolder();
  if (!request || request.output_dir !== '/tmp/parent' || request.mode !== 'create') {
    throw new Error(JSON.stringify(request));
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""))

    def test_archive_dialog_traps_tab_focus(self):
        run_node(app_harness("""
const overlay = element('archive-folder-decision');
const modal = element('archive-modal');
const first = element('archive-action-incremental');
const last = element('archive-action-reselect');
modal.children = [first, last];
overlay.querySelector = () => modal;
overlay.hidden = false;
document.activeElement = last;
let prevented = false;
Ptu.handleArchiveDialogKeydown({ key:'Tab', shiftKey:false, preventDefault() { prevented=true; } });
if (!prevented || document.activeElement !== first) throw new Error('Tab 未回到首个动作');
"""))


class FocusedWorkflowCssTests(unittest.TestCase):
    def test_focused_workflow_css_has_hidden_and_narrow_screen_guards(self):
        source = APP_CSS.read_text(encoding="utf-8")
        for token in (
            ".login-menu[hidden]",
            ".focused-workflow",
            ".workflow-toolbar",
            "@media (max-width: 1100px)",
        ):
            self.assertIn(token, source)

    def test_settings_group_is_not_glass(self):
        source = APP_CSS.read_text(encoding="utf-8")
        match = re.search(r"\.settings-grid\s*\{([^}]*)\}", source, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("var(--wb-surface)", match.group(1))
        self.assertNotIn("backdrop-filter", match.group(1))

    def test_main_scope_layout_survives_archive_dialog_styles(self):
        source = APP_CSS.read_text(encoding="utf-8")
        for selector in (".scope-date-grid", ".inline-number", ".scope-all-warning"):
            self.assertRegex(source, re.escape(selector) + r"\s*\{[^}]+\}")

    def test_archive_dialog_respects_reduced_motion_guard(self):
        source = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertIn("animation-duration: 0.01ms !important", source)


if __name__ == "__main__":
    unittest.main()
