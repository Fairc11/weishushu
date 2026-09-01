from pathlib import Path

from test_focused_workflow import app_harness, run_node
from frontend_assets import css_bundle_asset, frontend_bundle_asset


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "backend" / "app" / "templates" / "index.html"
APP_JS = frontend_bundle_asset()
APP_CSS = css_bundle_asset()


def test_slow_b_controls_and_progress_card_use_locked_text():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for token in (
        'name="archive-pacing-mode" value="standard" checked',
        'name="archive-pacing-mode" value="low_2_3_hours"',
        'name="archive-pacing-mode" value="low_4_6_hours"',
        'name="archive-pacing-mode" value="low_8_12_hours"',
        "标准速度",
        "约 2～3 小时",
        "约 4～6 小时",
        "约 8～12 小时",
        "接通电源时保持系统清醒，允许显示器关闭",
        'id="low-intensity-progress"',
        "分散请求间隔",
        "按请求类型分级",
        "限流时暂停",
        "阶段恢复点",
        "目标区间不是完成时间承诺",
    ):
        assert token in html


def test_user_interface_does_not_contain_forbidden_marketing_terms():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (INDEX_HTML, APP_JS, APP_CSS)
    )
    for forbidden in ("安全模式", "防封", "养号", "模拟真人"):
        assert forbidden not in source


def test_low_mode_sends_exact_selection_and_standard_remains_default():
    run_node(app_harness("""
Ptu.State.backupFolderDir = '/tmp/book';
let checked = null;
document.querySelector = (selector) => selector === 'input[name="archive-pacing-mode"]:checked' ? checked : null;
let request = null;
Ptu.Api.backupStart = async (body) => { request = body; return { task_id:'task-1', self_screen_name:'本人' }; };
Ptu.subscribeTask = () => {};
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.closeArchiveFolderDecision = () => {};
Ptu.toast = () => {};
(async () => {
  await Ptu.startArchiveBackup('incremental');
  if (request.pacing_mode !== 'standard' || request.keep_awake_when_plugged !== false) throw new Error(JSON.stringify(request));
  Ptu.clearActiveTask();
  checked = { value:'low_4_6_hours' };
  element('archive-keep-awake').checked = true;
  await Ptu.startArchiveBackup('rebuild');
  if (request.pacing_mode !== 'low_4_6_hours' || request.keep_awake_when_plugged !== true) throw new Error(JSON.stringify(request));
})().catch((error) => { console.error(error); process.exit(1); });
"""))


def test_pacing_websocket_updates_only_low_intensity_card():
    run_node(app_harness("""
let progressCalls = 0;
Ptu.setProgress = () => { progressCalls += 1; };
const card = element('low-intensity-progress');
card.hidden = true;
Ptu.handleTaskMessage({
  type:'pacing', mode:'low_4_6_hours', state:'waiting',
  request_kind:'comments', next_wait_seconds:38,
  disclaimer:'目标区间不是完成时间承诺',
});
if (progressCalls !== 0) throw new Error('pacing 覆盖了阶段进度');
if (card.hidden) throw new Error('低强度卡片未显示');
if (element('low-intensity-mode').textContent !== '约 4～6 小时') throw new Error('档位错误');
if (!element('low-intensity-next-wait').textContent.includes('38 秒')) throw new Error('等待错误');
Ptu.handleTaskMessage({ type:'pacing', mode:'standard', state:'standard', next_wait_seconds:null });
if (!card.hidden) throw new Error('标准模式未隐藏卡片');
"""))


def test_recovery_card_shows_original_mode_without_edit_control():
    run_node(app_harness("""
const task = {
  task_id:'0123456789ab', task_kind:'personal_archive', mode:'incremental',
  pacing_mode:'low_8_12_hours', state:'waiting_resume', phase:'sync',
  progress_current:12, progress_total:20, progress_unit:'post',
  saved_at:'2026-07-17T22:41:00+09:00', saved_content:'已保存 12 条微博',
};
Ptu.showRecoveryTask(task);
const summary = element('recovery-task-summary').textContent;
if (!summary.includes('低强度模式 · 约 8～12 小时')) throw new Error(summary);
if (summary.includes('标准速度')) throw new Error('恢复档位被重选');
"""))
