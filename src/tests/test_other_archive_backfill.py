"""部分时间建档后，他人入口与书库继续回填的回归。"""
from datetime import date, timedelta

import pytest
from playwright.sync_api import sync_playwright

from test_backup_flow_ui import load_page, stub_common
from test_staged_backup import FakeSource, IdentityProvider, item, post, known_bids, read_meta
from weibo_book.archive.discovery import ProfilePage
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.sync import PersonalArchiveSync, _months_ago


def test_library_exposes_partial_history_state(tmp_path, monkeypatch):
    from backend.app.services import archive_library
    root=tmp_path/'book'
    repo=ArchiveRepository.create(root, '20001', '合成博主')
    repo.write_history_complete(False)
    repo.write_history_cutoff('2025-09-12')
    repo.close()
    monkeypatch.setattr(archive_library, '_registry_path', lambda:tmp_path/'registry.json')
    archive_library.register_archive(str(root),'20001')
    entry=archive_library.list_archives()[0]
    assert entry.history_complete is False
    assert entry.history_cutoff == '2025-09-12'


@pytest.fixture
def browser(monkeypatch):
    # 前序封包路径探针会设置临时浏览器根；本组测试使用源码环境默认安装。
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.mark.parametrize('origin', ['other','library'])
@pytest.mark.parametrize('months', [12,24,0])
def test_partial_archive_can_continue_backfill_from_both_entries(browser, origin, months):
    page=browser.new_page()
    try:
        load_page(page);stub_common(page)
        page.evaluate("Ptu.bindEvents()")
        page.evaluate('''()=>{
          Ptu.State.loggedIn=true; Ptu.State.loginUid='10001';
          window.inspection={state:'archive',path:'/synthetic/book',uid:'20001',screen_name:'合成博主',history_complete:false,history_cutoff:'2025-09-12',total_posts:12,output_formats:{pdf:false,markdown:false}};
          Ptu.Api.backupInspect=async()=>inspection;
          Ptu.Api.libraryList=async()=>({entries:[inspection]});
          Ptu.Api.backupStart=async req=>{window.started=req; return {task_id:'t',self_screen_name:'合成博主'}};
        }''')
        # v2.2.0：两处入口都打开同一个“备份设置”抽屉，在抽屉里选择往前补充的范围
        if origin=='library':
            page.evaluate("Ptu.switchWorkspaceTab('library')")
            page.locator('#history-list').get_by_role('button',name='备份设置',exact=True).click()
        else:
            page.evaluate("Ptu.switchWorkspaceTab('other'); Ptu.selectBloggerTarget({uid:'20001',screen_name:'合成博主'}); Ptu.showPickedTargetInspection(inspection)")
            assert page.locator('#target-backfill-row').is_visible()
            assert '2025-09-12' in page.locator('#target-backfill-status').inner_text()
            page.locator('#btn-target-backfill-open').click()
        assert page.locator('#archive-drawer').is_visible()
        assert page.locator('#drawer-range-backfill').is_visible()
        assert page.locator('#drawer-range-create').is_hidden()
        assert '2025-09-12' in page.locator('#archive-drawer-status').inner_text()
        page.locator(f'#drawer-backfill-chips [data-months="{months}"]').click()
        page.locator('#drawer-backfill-start').click()
        page.wait_for_function('window.started !== undefined')
        req=page.evaluate('started')
        assert req['mode']=='backfill'
        assert req['target_uid']=='20001'
        assert req['output_dir']=='/synthetic/book'
        assert req['backfill_months']==months
        assert 'range_months' not in req
    finally:
        page.close()


def test_complete_archive_hides_backfill_in_both_entries(browser):
    page=browser.new_page()
    try:
        load_page(page);stub_common(page)
        page.evaluate("Ptu.bindEvents()")
        page.evaluate('''()=>{
          const value={state:'archive',path:'/book',uid:'20001',screen_name:'合成',history_complete:true};
          Ptu.switchWorkspaceTab('other'); Ptu.selectBloggerTarget({uid:'20001',screen_name:'合成'});
          Ptu.showPickedTargetInspection(value); Ptu.renderArchiveLibrary([value]);
        }''')
        assert page.locator('#target-backfill-row').is_hidden()
        assert page.locator('#history-list').get_by_role('button',name='补充更早微博',exact=True).count()==0
        # 已补全的档案打开抽屉只显示“历史已补全”，不提供补充范围
        page.evaluate("v => Ptu.openArchiveDrawer({target:{uid:'20001',screen_name:'合成'}, dir:'/book', inspection:{...v}})", {'state':'archive','path':'/book','uid':'20001','screen_name':'合成','history_complete':True})
        assert page.locator('#drawer-range-complete').is_visible()
        assert page.locator('#drawer-range-backfill').is_hidden()
    finally:
        page.close()


def test_repeated_year_backfills_extend_saved_boundary_without_rebuild(tmp_path):
    today=date.today()
    dates=[today-timedelta(days=3), _months_ago(today,18), _months_ago(today,36), _months_ago(today,72)]
    bids=['RECENT','YEAR_TWO','YEAR_FOUR','OLDEST']
    items=[item(bid,day) for bid,day in zip(bids,dates)]
    posts={bid:post(bid,day) for bid,day in zip(bids,dates)}
    root=tmp_path/'archive'
    source=FakeSource([ProfilePage(items,is_last=False)],posts)
    PersonalArchiveSync(root,source,IdentityProvider(),range_months=12).run('create')
    assert known_bids(root)=={'RECENT'}
    previous_cutoff=_months_ago(today,12)
    for months, expected_months, expected in [(12,24,{'RECENT','YEAR_TWO'}),(24,48,{'RECENT','YEAR_TWO','YEAR_FOUR'})]:
        PersonalArchiveSync(root,source,IdentityProvider(),backfill_months=months).run('backfill')
        assert known_bids(root)==expected
        previous_cutoff=_months_ago(previous_cutoff,months)
        assert read_meta(root)==(previous_cutoff.isoformat(),False)
    source=FakeSource([ProfilePage(items,is_last=True)],posts)
    PersonalArchiveSync(root,source,IdentityProvider(),backfill_months=0).run('backfill')
    assert known_bids(root)==set(bids)
    assert read_meta(root)==(None,True)


@pytest.mark.asyncio
async def test_completed_task_returns_real_archive_directory_and_identity(tmp_path,monkeypatch):
    from backend.app.services.personal_archive_tasks import PersonalArchiveTaskService
    from backend.app.services.persistent_task_store import PersistentTaskStore
    from backend.app.services.task_manager import TaskManager
    from backend.app.services import archive_library
    from backend.app.schemas import PersonalArchiveRequest
    manager=TaskManager(PersistentTaskStore(tmp_path/'task.json'))
    monkeypatch.setattr(archive_library,'_registry_path',lambda:tmp_path/'library.json')
    source=FakeSource([ProfilePage([item('A',date.today())],is_last=True)],{'A':post('A',date.today())})
    service=PersonalArchiveTaskService(manager=manager,dependency_builder=lambda *a,**k:(source,None),render_func=lambda *a,**k:[])
    parent=tmp_path/'books';parent.mkdir()
    started=await service.start(PersonalArchiveRequest(output_dir=str(parent),mode='create',pacing_mode='standard',keep_awake_when_plugged=False),IdentityProvider().whoami())
    await started.worker
    snapshot=manager.snapshot(started.task_id)
    assert snapshot['state']=='done',snapshot
    result=snapshot['result']
    assert result['uid']=='10001'
    assert result['output_dir']==str(parent/'测试用户_10001')
    assert result['output_dir']==snapshot['output_dir']


@pytest.mark.parametrize('mode',['create','backfill'])
def test_last_page_crossing_cutoff_does_not_claim_history_complete(tmp_path, mode):
    today=date.today(); recent=today-timedelta(days=3); oldest=_months_ago(today,72)
    root=tmp_path/'archive'
    source=FakeSource([ProfilePage([item('RECENT',recent),item('OLDEST',oldest)],is_last=True)],
                      {'RECENT':post('RECENT',recent),'OLDEST':post('OLDEST',oldest)})
    if mode=='create':
        PersonalArchiveSync(root,source,IdentityProvider(),range_months=12).run('create')
    else:
        repo=ArchiveRepository.create(root,'10001','测试用户')
        repo.write_history_cutoff(_months_ago(today,12).isoformat());repo.write_history_complete(False);repo.close()
        PersonalArchiveSync(root,source,IdentityProvider(),backfill_months=12).run('backfill')
    assert 'OLDEST' not in known_bids(root)
    assert read_meta(root)[1] is False
