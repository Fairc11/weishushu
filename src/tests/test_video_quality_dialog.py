"""真实浏览器验证备份前数量选择，使用合成响应，不读取真实档案。"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from backend.app.main import app
from tests.frontend_assets import frontend_bundle_asset, css_bundle_asset


def prepare(page, count, checking=False):
    page.set_default_timeout(5000)
    with TestClient(app) as client:
        html=client.get('/').text
    html=re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', html)
    html=re.sub(r'<link\b[^>]*>', '', html)
    page.set_content(html)
    page.add_style_tag(content=css_bundle_asset().read_text(encoding='utf-8'))
    page.add_script_tag(content=frontend_bundle_asset().read_text(encoding='utf-8'))
    page.evaluate('''({count,checking})=>{
      window.started=[];window.checkCancelled=false;
      for(const name of ['updateTaskActionAvailability','setActiveTask','setProgress','subscribeTask','setTaskCancelVisible','renderPacingStatus','safeLocalSet','closeArchiveFolderDecision'])Ptu[name]=()=>{};
      Ptu.toast=()=>{};Ptu.guardActiveTask=()=>false;
      Ptu.State.backupFolderDir='/synthetic/archive';
      Ptu.Api={cancelTask:async()=>{checkCancelled=true;return{cancelled:true}},taskStatus:async()=>({state:checkCancelled?'cancelled':'running',progress_msg:'检查中'}),backupVideoQualityCheck:async()=>({task_id:checking?'check-job':null,summary:{available_count:count,unchecked_count:0,failed_count:0}}),backupStart:async req=>{started.push(req);return{task_id:'task',self_screen_name:'测试'}}};
      window.startPromise=Ptu.startArchiveBackup('incremental');
    }''',{'count':count,'checking':checking})


@pytest.mark.parametrize('count,values',[(0,[]),(7,['0','all']),(10,['0','all']),(50,['0','10','all']),(65,['0','10','50','all'])])
def test_quantity_options_are_explicit_and_do_not_exceed_count(count,values):
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page()
        prepare(page,count)
        if count==0:
            page.wait_for_function('started.length===1')
            assert page.locator('#video-quality-decision').is_hidden()
            assert page.evaluate('started[0].video_upgrade_limit')==0
        else:
            page.locator('#video-quality-options').wait_for(state='visible')
            assert page.locator('input[name="video-upgrade-limit"]').evaluate_all('xs=>xs.map(x=>x.value)')==values
            assert page.evaluate('started.length')==0
            page.locator('input[name="video-upgrade-limit"][value="all"]').check()
            page.locator('#video-quality-confirm').click()
            page.wait_for_function('started.length===1')
            assert page.evaluate('started[0].video_upgrade_limit')=='all'
        browser.close()


def test_closing_choice_does_not_start_backup():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page();prepare(page,30)
        page.locator('#video-quality-options').wait_for(state='visible')
        page.locator('#video-quality-close').click()
        page.wait_for_function('!Ptu.State.backupStartBusy')
        assert page.evaluate('started.length')==0
        browser.close()


def test_closing_in_progress_check_cancels_without_starting_backup():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page();prepare(page,30,checking=True)
        page.locator('#video-quality-close').click()
        page.wait_for_function('!Ptu.State.backupStartBusy')
        assert page.evaluate('checkCancelled') is True
        assert page.evaluate('started.length')==0
        browser.close()
