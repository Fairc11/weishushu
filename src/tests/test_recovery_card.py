"""启动恢复卡片的离线初始化与四项动作契约。"""

from pathlib import Path
from frontend_assets import frontend_bundle_asset

from test_focused_workflow import app_harness, run_node


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "backend" / "app" / "templates" / "index.html"
APP_JS = frontend_bundle_asset()
API_CLIENT_JS = ROOT / "backend" / "app" / "static" / "js" / "api_client.js"


def test_recovery_card_has_locked_resume_b_nodes_and_actions():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for token in (
        'id="recovery-task-overlay"',
        'id="recovery-task-summary"',
        'id="recovery-task-nonrecoverable"',
        'id="recovery-task-resume"',
        'id="recovery-task-later"',
        'id="recovery-task-open-saved"',
        'id="recovery-task-abandon"',
        "继续任务",
        "稍后处理",
        "查看已保存内容",
        "放弃未完成部分",
        "本次错误不能继续任务",
    ):
        assert token in html


def test_recovery_api_methods_are_explicit_and_do_not_reuse_backup_start():
    source = API_CLIENT_JS.read_text(encoding="utf-8")
    for method in ("recoveryTask", "pauseTask", "resumeTask", "abandonTask"):
        assert f"{method}(" in source


def test_initial_recovery_check_only_reads_summary_and_does_not_resume():
    run_node(app_harness("""
let recoveryCalls = 0;
let resumeCalls = 0;
let backupStartCalls = 0;
Ptu.Api.recoveryTask = async () => {
  recoveryCalls += 1;
  return { task: {
    task_id:'0123456789ab', task_kind:'personal_archive', mode:'incremental',
    state:'waiting_resume', phase:'sync', progress_current:12,
    progress_total:null, progress_unit:'post',
    saved_at:'2026-07-17T22:41:00+09:00', pause_reason:'unexpected_exit',
    saved_content:'已保存 12 条微博'
  } };
};
Ptu.Api.resumeTask = async () => { resumeCalls += 1; };
Ptu.Api.backupStart = async () => { backupStartCalls += 1; };
(async () => {
  await Ptu.checkRecoveryTask();
  if (recoveryCalls !== 1 || resumeCalls !== 0 || backupStartCalls !== 0) throw new Error('启动时发生了自动继续');
  if (element('recovery-task-overlay').hidden) throw new Error('恢复卡片未显示');
  const summary = element('recovery-task-summary').textContent;
  if (!summary.includes('增量更新') || !summary.includes('已完成 12 条 · 总量未知') || !summary.includes('已保存 12 条微博')) throw new Error(summary);
})().catch((error) => { console.error(error); process.exit(1); });
"""))


def test_later_hides_card_but_resume_reuses_original_task_id():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'create', state:'waiting_resume', phase:'sync', progress_current:1, progress_total:3, progress_unit:'post', saved_at:'2026-07-17T00:00:00+00:00', pause_reason:'unexpected_exit', saved_content:'已保存 1 条微博' };
Ptu.showRecoveryTask(task);
Ptu.deferRecoveryTask();
if (!element('recovery-task-overlay').hidden || Ptu.State.recoveryTask.task_id !== task.task_id) throw new Error('稍后处理丢失了记录');
let resumed = null;
let subscribed = null;
Ptu.Api.resumeTask = async (taskId) => { resumed = taskId; return { task_id:taskId, state:'running' }; };
Ptu.subscribeTask = (taskId) => { subscribed = taskId; };
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
Ptu.showRecoveryTask(task);
(async () => {
  await Ptu.resumeRecoveryTask();
  if (resumed !== task.task_id || subscribed !== task.task_id || Ptu.State.taskId !== task.task_id) throw new Error('没有继续原任务');
})().catch((error) => { console.error(error); process.exit(1); });
"""))


def test_following_recovery_card_uses_explicit_task_label():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'following_archive', mode:'update', state:'waiting_resume', phase:'supertopics', progress_current:2, progress_total:null, progress_unit:'page', saved_at:'2026-07-18T00:00:00+00:00', pause_reason:'unexpected_exit', saved_content:'已暂存关注博主清单', pacing_mode:'standard' };
Ptu.showRecoveryTask(task);
const summary = element('recovery-task-summary').textContent;
if (!summary.includes('关注资料 · 更新关注资料')) throw new Error(summary);
"""))


def test_other_blogger_recovery_card_uses_target_label():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'create', state:'waiting_resume', phase:'sync', progress_current:539, progress_total:1202, progress_unit:'post', saved_at:'2026-09-01T16:30:27+08:00', pause_reason:'unexpected_exit', saved_content:'已提取 539/1202 条微博', pacing_mode:'standard', target_label:'郭德纲' };
Ptu.showRecoveryTask(task);
const summary = element('recovery-task-summary').textContent;
if (!summary.includes('@郭德纲 的微博书 · 首次建立')) throw new Error(summary);
if (summary.includes('本人微博书')) throw new Error(summary);
"""))


def test_self_recovery_card_without_target_label_keeps_self_label():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'incremental', state:'waiting_resume', phase:'sync', progress_current:3, progress_total:10, progress_unit:'post', saved_at:'2026-09-01T16:30:27+08:00', pause_reason:'unexpected_exit', saved_content:'已保存 3 条微博', pacing_mode:'standard', target_label:null };
Ptu.showRecoveryTask(task);
const summary = element('recovery-task-summary').textContent;
if (!summary.includes('本人微博书 · 增量更新')) throw new Error(summary);
"""))


def test_resume_marks_recovered_task_active_before_request_finishes():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'create', state:'waiting_resume', phase:'sync', progress_current:1, progress_total:null, progress_unit:'page', saved_at:'2026-07-17T00:00:00+00:00', pause_reason:'unexpected_exit', saved_content:'已保存 1 页' };
Ptu.showRecoveryTask(task);
let finishResume;
Ptu.Api.resumeTask = () => new Promise((resolve) => { finishResume = resolve; });
Ptu.subscribeTask = () => {};
Ptu.setProgress = () => {};
Ptu.setTaskCancelVisible = () => {};
const pending = Ptu.resumeRecoveryTask();
if (Ptu.State.taskId !== task.task_id) throw new Error('恢复请求期间没有标记活动任务');
finishResume({ task_id:task.task_id, state:'running' });
pending.catch((error) => { console.error(error); process.exit(1); });
"""))


def test_nonrecoverable_error_disables_resume_without_hiding_safe_abandon():
    run_node(app_harness("""
const task = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'incremental', state:'error', error_recoverable:false, phase:'sync', progress_current:4, progress_total:10, progress_unit:'post', saved_at:'2026-07-17T00:00:00+00:00', pause_reason:'归档数据校验失败', saved_content:'已保存 4 条微博' };
let resumeCalls = 0;
Ptu.Api.resumeTask = async () => { resumeCalls += 1; };
Ptu.showRecoveryTask(task);
const resume = element('recovery-task-resume');
const notice = element('recovery-task-nonrecoverable');
const abandon = element('recovery-task-abandon');
if (!resume.disabled) throw new Error('不可恢复错误仍可继续');
if (notice.hidden) throw new Error('缺少不可继续说明');
if (abandon.hidden || abandon.disabled) throw new Error('安全放弃入口不可用');
(async () => {
  await Ptu.resumeRecoveryTask();
  if (resumeCalls !== 0) throw new Error('不可恢复错误发送了继续请求');
  if (Ptu.State.taskId !== null) throw new Error('不可恢复错误被标记为活动任务');
  const recoverable = { ...task, error_recoverable:true };
  Ptu.Api.resumeTask = async () => { resumeCalls += 1; return { task_id:task.task_id, state:'running' }; };
  Ptu.subscribeTask = () => {};
  Ptu.setProgress = () => {};
  Ptu.setTaskCancelVisible = () => {};
  Ptu.showRecoveryTask(recoverable);
  if (resume.disabled || !notice.hidden) throw new Error('可恢复错误未提供继续入口');
  await Ptu.resumeRecoveryTask();
  if (resumeCalls !== 1) throw new Error('可恢复错误没有发送继续请求');
})().catch((error) => { console.error(error); process.exit(1); });
"""))


def test_recovery_state_actions_share_one_busy_lock_and_restore_exact_buttons():
    run_node(app_harness("""
const recoverable = { task_id:'0123456789ab', task_kind:'personal_archive', mode:'incremental', state:'error', error_recoverable:true, phase:'sync', progress_current:4, progress_total:10, progress_unit:'post', saved_at:'2026-07-17T00:00:00+00:00', pause_reason:'归档数据校验失败', saved_content:'已保存 4 条微博' };
const resume = element('recovery-task-resume');
const abandon = element('recovery-task-abandon');
const later = element('recovery-task-later');
const openSaved = element('recovery-task-open-saved');
let resumeCalls = 0;
let blockedAbandonCalls = 0;
let rejectResume;
Ptu.Api.resumeTask = () => {
  resumeCalls += 1;
  return new Promise((_resolve, reject) => { rejectResume = reject; });
};
Ptu.Api.abandonTask = async () => { blockedAbandonCalls += 1; };
Ptu.showRecoveryTask(recoverable);
(async () => {
  const firstResume = Ptu.resumeRecoveryTask();
  const duplicateResume = Ptu.resumeRecoveryTask();
  const blockedAbandon = Ptu.abandonRecoveryTask();
  await Promise.resolve();
  if (resumeCalls !== 1 || blockedAbandonCalls !== 0 || !Ptu.State.recoveryActionBusy) throw new Error('继续忙碌锁未阻止并发状态请求');
  if (!resume.disabled || !abandon.disabled) throw new Error('继续期间未锁定状态变更动作');
  if (later.disabled || openSaved.disabled) throw new Error('本地非状态动作被错误锁定');
  rejectResume(new Error('模拟继续失败'));
  await firstResume;
  await duplicateResume;
  await blockedAbandon;
  if (Ptu.State.recoveryActionBusy || resume.disabled || abandon.disabled) throw new Error('可恢复错误的按钮未恢复');

  const nonrecoverable = { ...recoverable, error_recoverable:false };
  let abandonCalls = 0;
  let rejectAbandon;
  Ptu.Api.abandonTask = () => {
    abandonCalls += 1;
    return new Promise((_resolve, reject) => { rejectAbandon = reject; });
  };
  Ptu.showRecoveryTask(nonrecoverable);
  const firstAbandon = Ptu.abandonRecoveryTask();
  const duplicateAbandon = Ptu.abandonRecoveryTask();
  const blockedResume = Ptu.resumeRecoveryTask();
  await Promise.resolve();
  if (abandonCalls !== 1 || resumeCalls !== 1 || !Ptu.State.recoveryActionBusy) throw new Error('放弃忙碌锁未阻止并发状态请求');
  if (!resume.disabled || !abandon.disabled) throw new Error('放弃期间未锁定状态变更动作');
  rejectAbandon(new Error('模拟放弃失败'));
  await firstAbandon;
  await duplicateAbandon;
  await blockedResume;
  if (Ptu.State.recoveryActionBusy || !resume.disabled || abandon.disabled) throw new Error('不可恢复错误的按钮恢复不正确');
})().catch((error) => { console.error(error); process.exit(1); });
"""))
