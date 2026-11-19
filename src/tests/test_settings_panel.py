"""v2.1.0 设置面板前端状态测试（node 聚合束）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_focused_workflow import app_harness, run_node


def test_settings_toggle_and_check_update_flow():
    run_node(app_harness('''
element('settings-overlay').hidden = true;
Ptu.toggleSettings();
if (element('settings-overlay').hidden) throw Error('设置面板未打开');
if (element('btn-install-update').hidden !== true) throw Error('安装按钮应默认隐藏');
Ptu.Api.updateCheck = async () => ({current:'2.1.0', latest:'v9.9.9', has_update:true, can_auto_install:true, dmg_asset:'x.dmg'});
(async () => {
  await Ptu.checkForUpdates();
  if (!element('update-status').textContent.includes('v9.9.9')) throw Error('未提示新版本');
  if (element('btn-install-update').hidden) throw Error('可自动安装时应显示安装按钮');
  Ptu.toggleSettings(false);
  if (!element('settings-overlay').hidden) throw Error('设置面板未关闭');
})();
'''))


def test_settings_check_update_without_auto_install_points_to_release_page():
    run_node(app_harness('''
Ptu.Api.updateCheck = async () => ({current:'2.1.0', latest:'v9.9.9', has_update:true, can_auto_install:false, dmg_asset:null});
(async () => {
  await Ptu.checkForUpdates();
  if (!element('update-status').textContent.includes('发布页')) throw Error('不可自动安装时应提示发布页');
  if (!element('btn-install-update').hidden) throw Error('不可自动安装时不得显示安装按钮');
})();
'''))


def test_install_update_stages_applies_and_closes_window():
    run_node(app_harness('''
const calls = [];
Ptu.guardActiveTask = () => false;
global.confirm = () => true;
Ptu.State.updateInfo = {has_update:true, latest:'v9.9.9'};
Ptu.Api.updateInstall = async () => { calls.push('install'); return {staged_path:'/tmp/Weishushu-update-1.app'}; };
Ptu.Api.updateApply = async (path) => { calls.push('apply:' + path); return {restarting:true}; };
Ptu.Api.closeWindow = async () => { calls.push('close'); return true; };
(async () => {
  await Ptu.installUpdate();
  if (calls.join(',') !== 'install,apply:/tmp/Weishushu-update-1.app,close') throw Error('更新流程顺序错误：' + calls.join(','));
})();
'''))


def test_auto_check_shows_update_prompt_with_plain_release_notes():
    run_node(app_harness('''
element('update-overlay').hidden = true;
element('risk-modal-overlay').hidden = true;
Ptu.settingsFlag = () => true;
Ptu.State.updateInfo = null; Ptu.State.updatePromptShown = false; Ptu.State.taskId = null;
Ptu.Api.updateCheck = async () => ({current:'2.2.0', latest:'v2.3.0', has_update:true,
  can_auto_install:true, dmg_asset:'x.dmg', notes:'## 新功能\\n- **重要**：需要 macOS 26'});
(async () => {
  await Ptu.maybeAutoCheckUpdate();
  if (element('update-overlay').hidden) throw Error('取到新版本后应弹窗');
  const notes = element('update-modal-notes').textContent;
  if (notes.includes('##') || notes.includes('**')) throw Error('更新说明应去掉 Markdown 标记');
  if (!notes.includes('需要 macOS 26')) throw Error('更新说明内容缺失');
  if (!element('update-modal-version').textContent.includes('v2.3.0')) throw Error('未显示新版本号');
  if (element('update-modal-install').hidden || !element('update-modal-open').hidden) throw Error('可自动安装时应显示“下载并更新”');
})();
'''))


def test_update_prompt_points_to_release_page_when_auto_install_unavailable():
    run_node(app_harness('''
element('update-overlay').hidden = true;
element('risk-modal-overlay').hidden = true;
Ptu.State.updateInfo = {current:'2.2.0', latest:'v2.3.0', has_update:true, can_auto_install:false, dmg_asset:null, notes:'说明'};
Ptu.State.updatePromptShown = false; Ptu.State.taskId = null;
Ptu.maybeShowUpdatePrompt();
if (element('update-overlay').hidden) throw Error('应弹窗');
if (!element('update-modal-install').hidden || element('update-modal-open').hidden) throw Error('不可自动安装时应显示“前往发布页下载”');
'''))


def test_update_prompt_respects_skip_task_and_network_failure():
    run_node(app_harness('''
element('update-overlay').hidden = true;
element('risk-modal-overlay').hidden = true;
const info = {current:'2.2.0', latest:'v2.3.0', has_update:true, can_auto_install:true, dmg_asset:'x.dmg', notes:''};
// 已跳过的版本不再弹窗
Ptu.State.updateInfo = info; Ptu.State.updatePromptShown = false; Ptu.State.taskId = null;
Ptu.safeLocalGet = key => key === 'weishushu.skippedUpdateVersion' ? 'v2.3.0' : null;
Ptu.maybeShowUpdatePrompt();
if (!element('update-overlay').hidden) throw Error('已跳过的版本不应弹窗');
// 任务进行中不打断
Ptu.safeLocalGet = () => null; Ptu.State.taskId = 'task-1';
Ptu.maybeShowUpdatePrompt();
if (!element('update-overlay').hidden) throw Error('任务进行中不应弹窗');
// 网络取不到发布信息时静默
Ptu.State.taskId = null; Ptu.State.updateInfo = null; Ptu.settingsFlag = () => true;
Ptu.Api.updateCheck = async () => { throw new Error('网络不可用'); };
(async () => {
  await Ptu.maybeAutoCheckUpdate();
  if (!element('update-overlay').hidden) throw Error('检查失败时不应弹窗');
  // “跳过此版本”记住版本号
  let saved = null; Ptu.safeLocalSet = (key, value) => { if (key === 'weishushu.skippedUpdateVersion') saved = value; };
  Ptu.State.updateInfo = info; element('update-overlay').hidden = false;
  Ptu.closeUpdatePrompt(true);
  if (saved !== 'v2.3.0' || !element('update-overlay').hidden) throw Error('跳过此版本未生效');
})();
'''))
