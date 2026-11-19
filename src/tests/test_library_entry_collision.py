"""书库与他人档案入口的逻辑对撞：守卫、双击、任务中禁用、历史未补全提示。"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from test_backup_flow_ui import load_page, stub_common


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        page.evaluate("window._realGuardActiveTask = Ptu.guardActiveTask")
        stub_common(page)
        # stub_common 会把守卫换成恒放行，对撞测试必须恢复真实守卫
        page.evaluate("Ptu.guardActiveTask = window._realGuardActiveTask")
        page.evaluate("Ptu.bindEvents()")
        yield page
        browser.close()


def _stub_library_apis(page, inspection_overrides=None):
    inspection = {
        'state': 'archive', 'path': '/book/a', 'uid': '20001', 'screen_name': '合成',
        'total_posts': 5, 'history_complete': True,
    }
    inspection.update(inspection_overrides or {})
    page.evaluate('''inspection => {
      window.starts = 0;
      window.inspectCalls = 0;
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.Api.backupInspect = async () => { window.inspectCalls++; return inspection; };
      Ptu.Api.backupStart = async () => { window.starts++; return {task_id:'task-'+window.starts, self_screen_name:'合成'}; };
      Ptu.Api.backupVideoQualityCheck = async () => ({task_id:null, summary:{available_count:0, unchecked_count:0, failed_count:0}});
    }''', inspection)


ENTRY = {'path': '/book/a', 'uid': '20001', 'screen_name': '合成', 'available': True, 'total_posts': 5}


def test_double_click_library_update_starts_only_once(page):
    _stub_library_apis(page)
    page.evaluate("entry => { Ptu.updateLibraryEntry(entry); Ptu.updateLibraryEntry(entry); }", ENTRY)
    page.wait_for_function("window.starts === 1")
    page.wait_for_function("window.toasts.some(m => m.includes('已有任务'))")
    assert page.evaluate("window.starts") == 1


def test_double_click_supertopic_supplement_starts_only_once(page):
    _stub_library_apis(page)
    verified_entry = {**ENTRY, 'verified': True}
    page.evaluate("entry => { Ptu.updateLibraryEntry(entry, true); Ptu.updateLibraryEntry(entry, true); }", verified_entry)
    page.wait_for_function("window.starts === 1")
    assert page.evaluate("window.starts") == 1


def test_library_buttons_disabled_while_task_running(page):
    page.evaluate('''entry => {
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.State.taskId = 'running-task';
      Ptu.Api.resolveTarget = async () => ({uid:'20001', screen_name:'合成', verified:true});
      Ptu.renderArchiveLibrary([{...entry, history_complete:false}]);
    }''', ENTRY)
    page.wait_for_function("document.querySelectorAll('#history-list [data-library-update]').length === 3")
    states = page.evaluate('''() => Array.from(document.querySelectorAll('#history-list [data-library-update]')).map(b => b.disabled)''')
    assert states == [True, True, True]
    open_disabled = page.evaluate('''() => Array.from(document.querySelectorAll('#history-list button')).find(b => b.textContent === '打开位置').disabled''')
    assert open_disabled is False


def test_settings_button_for_every_available_archive(page):
    page.evaluate('''entry => {
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.renderArchiveLibrary([
        {...entry, path:'/book/full', history_complete:true},
        {...entry, path:'/book/partial', history_complete:false},
      ]);
    }''', ENTRY)
    # v2.2.0：每本可用的微博书都有“备份设置”，补充范围在抽屉里按档案状态显示
    items = page.locator('#history-list .history-item')
    assert items.nth(0).get_by_role('button', name='备份设置').count() == 1
    assert items.nth(1).get_by_role('button', name='备份设置').count() == 1
    assert items.nth(0).get_by_role('button', name='补充更早微博').count() == 0


def test_settings_entry_shows_complete_state_for_complete_archive(page):
    _stub_library_apis(page, {'history_complete': True})
    page.evaluate('''() => {
      window.tabSwitches = [];
      const original = Ptu.switchWorkspaceTab.bind(Ptu);
      Ptu.switchWorkspaceTab = tab => { window.tabSwitches.push(tab); original(tab); };
    }''')
    page.evaluate("entry => Ptu.openLibraryBackfill(entry)", ENTRY)
    page.wait_for_function("!document.getElementById('archive-drawer').hidden")
    assert page.locator('#drawer-range-complete').is_visible()
    assert page.locator('#drawer-range-backfill').is_hidden()
    assert page.evaluate("window.tabSwitches") == []


def test_library_update_toasts_backfill_hint_for_incomplete_history(page):
    _stub_library_apis(page, {'history_complete': False})
    page.evaluate("entry => Ptu.updateLibraryEntry(entry)", ENTRY)
    page.wait_for_function("window.starts === 1")
    page.wait_for_function("window.toasts.some(m => m.includes('备份设置'))")


def test_relocate_and_remove_rejected_while_task_running(page):
    page.evaluate('''entry => {
      Ptu.State.taskId = 'running-task';
      window.relocateCalls = 0;
      window.removeCalls = 0;
      Ptu.selectLibraryDirectory = async () => { window.relocateCalls++; return '/elsewhere'; };
      Ptu.Api.libraryRelocate = async () => { window.relocateCalls++; return {entries:[]}; };
      Ptu.Api.libraryRemove = async () => { window.removeCalls++; return {entries:[]}; };
      Ptu.relocateLibraryEntry(entry);
      Ptu.removeLibraryEntry(entry);
    }''', ENTRY)
    page.wait_for_function("window.toasts.filter(m => m.includes('已有任务')).length === 2")
    assert page.evaluate("window.relocateCalls + window.removeCalls") == 0


def test_logged_out_entries_are_rejected_with_clear_message(page):
    page.evaluate('''entry => {
      Ptu.State.loggedIn = false;
      window.apiCalls = 0;
      Ptu.Api.backupInspect = async () => { window.apiCalls++; return {}; };
      Ptu.updateLibraryEntry(entry);
      Ptu.State.archiveTarget = {uid:'20001', screen_name:'合成', verified:true};
      Ptu.State.backupFolderInspection = {state:'archive', path:'/book/a', uid:'20001'};
      Ptu.startSupertopicSupplement();
    }''', ENTRY)
    page.wait_for_function("window.toasts.filter(m => m.includes('请先登录')).length === 2")
    assert page.evaluate("window.apiCalls") == 0
