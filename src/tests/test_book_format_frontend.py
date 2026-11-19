"""版式号前端：书库徽标、“升级版式”入口与他人档案版式提示。"""
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
        page.evaluate("window._realGuardActiveTask = Ptu.guardActiveTask; window._realRefreshSelf = Ptu.refreshSelfArchiveStatus")
        stub_common(page)
        page.evaluate("Ptu.guardActiveTask = window._realGuardActiveTask; Ptu.refreshSelfArchiveStatus = window._realRefreshSelf")
        page.evaluate("Ptu.bindEvents()")
        yield page
        browser.close()


def _entry(version, **changes):
    entry = {
        'path': f'/book/v{version}', 'uid': '20001', 'screen_name': '合成',
        'available': True, 'total_posts': 5, 'book_format_version': version,
    }
    entry.update(changes)
    return entry


def test_library_entries_show_format_badges(page):
    page.evaluate('''entries => {
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.renderArchiveLibrary(entries);
    }''', [_entry(2), _entry(3), _entry(4), _entry(1, available=False), _entry(None)])
    # 当前版式号由后端渲染进 body；低于它（且不低于 V2）的档案都可本地升级
    assert page.evaluate("Ptu.currentBookFormat()") == 4
    items = page.locator('#history-list .history-item')
    assert items.nth(0).locator('.library-format').text_content() == '版式 V2 · 可升级 V4'
    assert items.nth(1).locator('.library-format').text_content() == '版式 V3 · 可升级 V4'
    assert items.nth(2).locator('.library-format').text_content() == '版式 V4'
    assert items.nth(3).locator('.library-format').text_content() == '版式 V1 · 需重新备份'
    assert items.nth(4).locator('.library-format').count() == 0
    assert items.nth(0).get_by_role('button', name='升级版式').count() == 1
    assert items.nth(1).get_by_role('button', name='升级版式').count() == 1
    assert items.nth(2).get_by_role('button', name='升级版式').count() == 0
    assert items.nth(3).get_by_role('button', name='升级版式').count() == 0


def test_library_regenerate_calls_preview_with_target(page):
    page.evaluate('''() => {
      window.previews = [];
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.Api.backupPreview = async req => { window.previews.push(req); return {task_id:'task-1'}; };
    }''')
    page.evaluate("entry => Ptu.regenerateArchiveHtml(entry.path, entry.uid)", _entry(2))
    page.wait_for_function("window.previews.length === 1")
    assert page.evaluate("window.previews[0]") == {'output_dir': '/book/v2', 'target_uid': '20001'}
    page.evaluate("() => Ptu.regenerateArchiveHtml('/book/self', null)")
    page.wait_for_function("window.previews.length === 2")
    assert page.evaluate("window.previews[1]") == {'output_dir': '/book/self'}


def test_library_regenerate_rejected_while_task_running(page):
    page.evaluate('''entry => {
      window.previews = [];
      Ptu.State.taskId = 'running-task';
      Ptu.Api.backupPreview = async req => { window.previews.push(req); return {task_id:'x'}; };
      Ptu.regenerateArchiveHtml(entry.path, entry.uid);
    }''', _entry(2))
    page.wait_for_function("window.toasts.some(m => m.includes('已有任务'))")
    assert page.evaluate("window.previews.length") == 0


def test_self_preview_button_renamed(page):
    assert page.locator('#btn-archive-preview').text_content() == '更新 HTML（不联网）'


def test_other_archive_shows_format_and_regenerate_for_v2(page):
    page.evaluate('''() => {
      window.previews = [];
      Ptu.State.workspaceTab = 'other';
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.State.archiveTarget = {uid:'20001', screen_name:'合成', verified:false};
      Ptu.applyOutputFormatsToCard = () => {};
      Ptu.Api.backupPreview = async req => { window.previews.push(req); return {task_id:'task-2'}; };
      document.getElementById('step-2').hidden = false;
      document.getElementById('picked-blogger').hidden = false;
      Ptu.showPickedTargetInspection({
        state:'archive', path:'/book/other', uid:'20001', total_posts:9,
        last_successful_sync_at:'2026-09-01', history_complete:true,
        book_format_version: 2,
      });
    }''')
    status = page.locator('#picked-folder-status').text_content()
    assert '版式 V2' in status
    row = page.locator('#target-regenerate-row')
    assert row.is_visible()
    page.locator('#btn-target-regenerate').click()
    page.wait_for_function("window.previews.length === 1")
    assert page.evaluate("window.previews[0]") == {'output_dir': '/book/other', 'target_uid': '20001'}


def test_other_archive_current_format_hides_regenerate_row(page):
    page.evaluate('''() => {
      Ptu.State.workspaceTab = 'other';
      Ptu.State.archiveTarget = {uid:'20001', screen_name:'合成', verified:false};
      Ptu.applyOutputFormatsToCard = () => {};
      Ptu.showPickedTargetInspection({
        state:'archive', path:'/book/other', uid:'20001', total_posts:9,
        last_successful_sync_at:'2026-09-01', history_complete:true,
        book_format_version: 4,
      });
    }''')
    status = page.locator('#picked-folder-status').text_content()
    assert '版式 V4' in status and '可升级' not in status
    assert page.locator('#target-regenerate-row').is_hidden()


def test_other_archive_previous_format_shows_regenerate_row(page):
    page.evaluate('''() => {
      Ptu.State.workspaceTab = 'other';
      Ptu.State.archiveTarget = {uid:'20001', screen_name:'合成', verified:false};
      Ptu.applyOutputFormatsToCard = () => {};
      Ptu.showPickedTargetInspection({
        state:'archive', path:'/book/other', uid:'20001', total_posts:9,
        last_successful_sync_at:'2026-09-01', history_complete:true,
        book_format_version: 3,
      });
    }''')
    status = page.locator('#picked-folder-status').text_content()
    assert '版式 V3（可升级 V4）' in status
    # 测试页未切换到“备份他人”页签，只检查行本身不再隐藏
    assert page.locator('#target-regenerate-row').evaluate('row => row.hidden') is False
    assert 'V4' in page.locator('#target-regenerate-row').text_content()


def test_other_archive_reset_hides_regenerate_row(page):
    page.evaluate('''() => {
      Ptu.State.workspaceTab = 'other';
      Ptu.State.archiveTarget = {uid:'20001', screen_name:'合成', verified:false};
      Ptu.applyOutputFormatsToCard = () => {};
      Ptu.showPickedTargetInspection({
        state:'archive', path:'/book/other', uid:'20001', total_posts:9,
        last_successful_sync_at:'2026-09-01', history_complete:true,
        book_format_version: 2,
      });
      Ptu.resetPickedTargetFolder();
    }''')
    assert page.locator('#target-regenerate-row').is_hidden()


def test_self_archive_status_shows_format_badge(page):
    page.evaluate('''() => {
      Ptu.State.loggedIn = true;
      Ptu.State.loginUid = '10001';
      Ptu.State.workspaceTab = 'self';
      Ptu.State.selfArchiveDir = '/book/self';
      Ptu.applyOutputFormatsToCard = () => {};
      Ptu.Api.backupInspect = async () => ({
        state:'archive', path:'/book/self', uid:'10001', screen_name:'自己',
        total_posts:42, last_successful_sync_at:'2026-09-01', history_complete:true,
        book_format_version: 2,
      });
    }''')
    page.evaluate("Ptu.refreshSelfArchiveStatus()")
    page.wait_for_function("document.getElementById('self-archive-status').textContent.includes('版式 V2')")
    status = page.locator('#self-archive-status').text_content()
    assert '版式 V2（可升级 V4）' in status
