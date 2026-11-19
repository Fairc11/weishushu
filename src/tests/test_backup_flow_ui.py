"""第五轮界面修复：先选文件夹的备份流程、登录徽章昵称、微博书库。"""
import re

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from backend.app.main import app
from tests.frontend_assets import css_bundle_asset, frontend_bundle_asset


def load_page(page):
    page.set_default_timeout(5000)
    with TestClient(app) as client:
        html = client.get('/').text
    html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', html)
    html = re.sub(r'<link\b[^>]*>', '', html)
    page.set_content(html)
    page.add_style_tag(content=css_bundle_asset().read_text(encoding='utf-8'))
    page.add_script_tag(content=frontend_bundle_asset().read_text(encoding='utf-8'))


def stub_common(page):
    page.evaluate('''()=>{
      window.toasts=[];
      for(const name of ['updateTaskActionAvailability','setActiveTask','setProgress','subscribeTask','setTaskCancelVisible','renderPacingStatus','safeLocalSet','closeArchiveFolderDecision','refreshSelfArchiveStatus'])Ptu[name]=()=>{};
      Ptu.toast=(m)=>{toasts.push(m)};
      Ptu.guardActiveTask=()=>false;
    }''')


@pytest.mark.parametrize('state,expect', [
    ('archive', 'update'),
    ('empty', 'create'),
    ('ordinary_nonempty', 'create'),
    ('legacy_index', 'create'),
    ('uid_mismatch', 'none'),
    ('damaged', 'none'),
])
def test_target_flow_shows_actions_after_folder_inspection(state, expect):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate("Ptu.switchWorkspaceTab('other')")
        page.evaluate('''state=>{
          window.pywebview={api:{select_folder:async()=>'/picked'}};
          Ptu.Api.backupInspect=async()=>({state,path:'/picked/测试_1000000001',uid:'1000000001',screen_name:'测试',total_posts:12,last_successful_sync_at:'2026-09-01'});
          Ptu.selectBloggerTarget({uid:'1000000001',screen_name:'测试'});
        }''', state)
        assert page.locator('#picked-blogger').is_visible()
        assert page.locator('#picked-create-block').is_hidden()
        assert page.locator('#picked-update-block').is_hidden()
        page.evaluate('Ptu.pickTargetFolder()')
        page.wait_for_function('''()=>{
          const c=document.getElementById('picked-create-block');
          const u=document.getElementById('picked-update-block');
          const s=document.getElementById('picked-folder-status');
          return !c.hidden || !u.hidden || !s.hidden;
        }''')
        assert page.locator('#picked-create-block').is_visible() == (expect == 'create')
        assert page.locator('#picked-update-block').is_visible() == (expect == 'update')
        if state == 'archive':
            assert '已有微博书' in page.locator('#picked-folder-status').inner_text()
        if state == 'uid_mismatch':
            assert '不能覆盖' in page.locator('#picked-folder-status').inner_text() or '如要更新' in page.locator('#picked-folder-status').inner_text()
        browser.close()


def test_target_incremental_uses_selected_folder_without_range():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate('''()=>{
          window.started=[];
          Ptu.State.archiveTarget={uid:'1000000001',screen_name:'测试'};
          Ptu.State.backupFolderDir='/picked/测试_1000000001';
          Ptu.State.backupFolderInspection={state:'archive'};
          Ptu.Api.backupVideoQualityCheck=async()=>({task_id:null,summary:{available_count:0,unchecked_count:0,failed_count:0}});
          Ptu.Api.backupStart=async req=>{started.push(req);return{task_id:'t',self_screen_name:'测试'}};
        }''')
        page.evaluate('''()=>{Ptu.showPickedTargetInspection({state:'archive',path:'/picked/测试_1000000001',uid:'1000000001',screen_name:'测试',total_posts:12,last_successful_sync_at:''});
          Ptu.startArchiveBackup('incremental');}''')
        page.wait_for_function('started.length===1')
        req = page.evaluate('started[0]')
        assert req['mode'] == 'incremental'
        assert req['output_dir'] == '/picked/测试_1000000001'
        assert req['target_uid'] == '1000000001'
        assert 'range_months' not in req
        assert req['video_upgrade_limit'] == 0
        browser.close()


def test_login_badge_shows_account_name():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate('''()=>{
          Ptu.Api.loginStatus=async()=>({logged_in:true,cookie_source:'file'});
          Ptu.Api.whoami=async()=>({uid:'12345',screen_name:'一个很长的测试昵称'});
        }''')
        page.evaluate('Ptu.refreshLoginStatus()')
        page.wait_for_function("document.getElementById('login-badge').textContent.includes('测试昵称')")
        assert page.locator('#login-badge').inner_text() == '已登录 · 一个很长的测试昵称'
        assert '当前账号' in page.locator('#login-badge').get_attribute('title')
        assert page.evaluate('Ptu.State.loginUid') == '12345'
        browser.close()


def test_archive_library_lists_entries_and_update_flow():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate('''()=>{
          window.pywebview={api:{select_folder:async()=>'/library',show_in_folder:async()=>true}};
          Ptu.State.isDesktop=true;
          Ptu.State.loginUid='12345';
          Ptu.State.loggedIn=true;
          Ptu.Api.backupArchives=async()=>({entries:[
            {path:'/library/本人_12345',uid:'12345',screen_name:'本人',total_posts:100,last_successful_sync_at:'2026-09-09'},
            {path:'/library/测试_1000000001',uid:'1000000001',screen_name:'测试',total_posts:12,last_successful_sync_at:'2026-09-01'},
          ]});
          Ptu.Api.libraryRegister=async()=>({});
          Ptu.Api.libraryList=Ptu.Api.backupArchives;
          Ptu.Api.backupInspect=async req=>({state:'archive',path:req.output_dir,uid:req.target_uid || '12345',screen_name:'测试'});
          Ptu.Api.backupVideoQualityCheck=async()=>({task_id:null,summary:{available_count:0,unchecked_count:0,failed_count:0}});
          window.started=[];
          Ptu.Api.backupStart=async req=>{started.push(req);return {task_id:'test',self_screen_name:'测试'}};
        }''')
        page.evaluate('Ptu.toggleHistory(true)')
        page.evaluate('Ptu.pickHistoryFolder()')
        page.wait_for_function("document.querySelectorAll('#history-list .history-item').length===2")
        assert '测试' in page.locator('#history-list').inner_text()
        # 已登记他人档案直接复用目录续备，不再重新搜索和选目录。
        page.locator('#history-list .history-item').nth(1).locator('button', has_text='新增备份').click()
        page.wait_for_function('started.length===1')
        assert page.evaluate('started[0].mode') == 'incremental'
        assert page.evaluate('started[0].target_uid') == '1000000001'
        assert page.evaluate('started[0].target_uid') == '1000000001'
        assert page.evaluate('started[0].output_dir') == '/library/测试_1000000001'
        browser.close()
