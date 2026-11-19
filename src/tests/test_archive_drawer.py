"""v2.2.0 备份设置抽屉：一套规则统一三处入口的范围、节奏与保存格式。"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from test_backup_flow_ui import load_page, stub_common


@pytest.fixture
def page(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate("Ptu.bindEvents()")
        page.evaluate('''() => {
          Ptu.State.loggedIn = true; Ptu.State.loginUid = '10001';
          window.starts = [];
          Ptu.Api.backupStart = async req => { window.starts.push(req); return {task_id:'t' + window.starts.length, self_screen_name:'我'}; };
          Ptu.Api.backupVideoQualityCheck = async () => ({task_id:null, summary:{available_count:0, unchecked_count:0, failed_count:0}});
          // 测试页不允许访问 localStorage，改用内存存储模拟本机记录
          window.memory = {};
          Ptu.safeLocalGet = key => (key in window.memory ? window.memory[key] : null);
          Ptu.safeLocalSet = (key, value) => { window.memory[key] = value; };
        }''')
        yield page
        browser.close()


def _finish_task(page):
    page.evaluate("Ptu.State.taskId = null; Ptu.State.taskStartBusy = false; Ptu.State.backupStartBusy = false;")


def test_self_backfill_from_drawer(page):
    page.evaluate('''() => Ptu.openArchiveDrawer({target:null, dir:'/self', inspection:{
      state:'archive', path:'/self', uid:'10001', screen_name:'我', total_posts:20,
      history_complete:false, history_cutoff:'2025-11-01', output_formats:{pdf:false, markdown:false}}})''')
    assert page.locator('#drawer-range-backfill').is_visible()
    page.locator('#drawer-backfill-chips [data-months="3"]').click()
    page.locator('#drawer-backfill-start').click()
    page.wait_for_function("window.starts.length === 1")
    req = page.evaluate("window.starts[0]")
    assert req['mode'] == 'backfill' and req['backfill_months'] == 3
    assert req['output_dir'] == '/self' and 'target_uid' not in req


def test_self_first_backup_range_only_when_chosen_in_drawer(page):
    page.evaluate('''() => Ptu.openArchiveDrawer({target:null, dir:'/new', inspection:{state:'empty', path:'/new'}})''')
    assert page.locator('#drawer-range-create').is_visible()
    assert page.locator('#drawer-primary').inner_text() == '开始首次备份'
    page.locator('#drawer-create-chips [data-months="3"]').click()
    page.locator('#drawer-primary').click()
    page.wait_for_function("window.starts.length === 1")
    req = page.evaluate("window.starts[0]")
    assert req['mode'] == 'create' and req['range_months'] == 3 and 'target_uid' not in req


def test_self_first_backup_without_drawer_keeps_full_history(page):
    page.evaluate("Ptu.State.archiveTarget = null; Ptu.State.backupFolderDir = '/new'; Ptu.startArchiveBackup('create')")
    page.wait_for_function("window.starts.length === 1")
    assert 'range_months' not in page.evaluate("window.starts[0]")


def test_pacing_is_remembered_per_book(page):
    inspection = {'state': 'archive', 'path': '/a', 'uid': '20001', 'screen_name': '甲', 'history_complete': True,
                  'output_formats': {'pdf': False, 'markdown': False}}
    page.evaluate("v => Ptu.openArchiveDrawer({target:{uid:'20001', screen_name:'甲'}, dir:'/a', inspection:v})", inspection)
    assert page.locator('#drawer-range-complete').is_visible()
    page.locator('input[name="drawer-pacing-mode"][value="low_4_6_hours"]').check()
    page.locator('#drawer-primary').click()
    page.wait_for_function("window.starts.length === 1")
    assert page.evaluate("window.starts[0]")['pacing_mode'] == 'low_4_6_hours'
    _finish_task(page)
    # 再次打开同一本书：抽屉带出上次的节奏
    page.evaluate("v => Ptu.openArchiveDrawer({target:{uid:'20001', screen_name:'甲'}, dir:'/a', inspection:v})", inspection)
    assert page.locator('input[name="drawer-pacing-mode"][value="low_4_6_hours"]').is_checked()
    page.evaluate("Ptu.closeArchiveDrawer()")
    # 另一本书没有记录时为标准速度
    page.evaluate("v => Ptu.openArchiveDrawer({target:{uid:'20002', screen_name:'乙'}, dir:'/b', inspection:v})", {**inspection, 'path': '/b', 'uid': '20002'})
    assert page.locator('input[name="drawer-pacing-mode"][value="standard"]').is_checked()


def test_drawer_formats_save_to_this_book(page):
    page.evaluate('''() => {
      window.saved = null;
      Ptu.Api.backupSettingsSave = async req => { window.saved = req; return {output_formats:req.output_formats}; };
      Ptu.openArchiveDrawer({target:{uid:'20001', screen_name:'甲'}, dir:'/a', inspection:{
        state:'archive', path:'/a', uid:'20001', history_complete:true, output_formats:{pdf:false, markdown:false}}});
    }''')
    page.locator('#drawer-fmt-pdf').check()
    page.wait_for_function("window.saved !== null")
    assert page.evaluate("window.saved") == {'output_dir': '/a', 'output_formats': {'pdf': True, 'markdown': False}, 'target_uid': '20001'}
    # 还没建档时格式不可改
    page.evaluate("Ptu.openArchiveDrawer({target:null, dir:'/new', inspection:{state:'empty', path:'/new'}})")
    assert page.locator('#drawer-fmt-pdf').is_disabled()
