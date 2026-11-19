"""可见入口的跨状态链路回归：终态、暂停、登录与任务后续操作。"""
import pytest
from test_focused_workflow import app_harness, run_node


@pytest.mark.parametrize('state',['done','error','cancelled','waiting_resume'])
def test_terminal_snapshot_unblocks_ui_and_pause_opens_recovery(state):
    run_node(app_harness('''
Ptu.State.taskId='active';
let closed=0,recovery=0,rendered=0;
Ptu.setProgress=()=>{};Ptu.renderProgressEvent=()=>{};Ptu.setTaskCancelVisible=()=>{};
Ptu.renderResult=()=>{rendered++};Ptu.toast=()=>{};Ptu.hidePacingStatus=()=>{};
Ptu._closeTaskWs=()=>{closed++};Ptu.checkRecoveryTask=()=>{recovery++};
Ptu.refreshSelfArchiveStatus=()=>{};Ptu.refreshHistoryList=()=>{};
''' + f"Ptu.handleTaskMessage({{type:'snapshot',id:'active',state:'{state}',result:{{}},error:'测试'}});" + '''
if(Ptu.State.taskId!==null||closed!==1)throw Error('终态快照未释放任务');
''' + ("if(recovery!==1)throw Error('暂停后未显示恢复入口');" if state=='waiting_resume' else '') + ("if(rendered!==1)throw Error('完成快照未显示结果');" if state=='done' else '')))


def test_waiting_resume_event_reaches_recovery():
    run_node(app_harness('''
Ptu.State.taskId='active';let recovery=0;
Ptu.setProgress=()=>{};Ptu.setTaskCancelVisible=()=>{};Ptu.toast=()=>{};
Ptu.hidePacingStatus=()=>{};Ptu._closeTaskWs=()=>{};Ptu.checkRecoveryTask=()=>{recovery++};
Ptu.handleTaskMessage({type:'waiting_resume',pause_reason:'rate_limited'});
if(Ptu.State.taskId!==null||recovery!==1)throw Error('等待继续事件没有到达恢复入口');
'''))


@pytest.mark.parametrize('action',['logout','loginQrcode'])
def test_running_task_blocks_login_mutations(action):
    run_node(app_harness('''
Ptu.State.taskId='active';let calls=0;
Ptu.toast=()=>{};Ptu.Api.logout=async()=>{calls++};Ptu.Api.loginQrcode=async()=>{calls++;return {session_id:'test'}};
Ptu.refreshLoginStatus=async()=>{};
(async()=>{
''' + f'await Ptu.{action}();' + '''
if(calls!==0)throw Error('备份中仍改变登录态');
})();
'''))


def test_completion_refreshes_selected_other_archive_to_actual_child_directory():
    run_node(app_harness('''
const target={uid:'20001',screen_name:'目标'};
Ptu.State.workspaceTab='other';Ptu.State.otherArchiveContext={target,dir:'/parent',inspection:{state:'ordinary_nonempty',path:'/parent'}};
Ptu.State.archiveTarget=target;Ptu.State.backupFolderDir='/parent';Ptu.State.taskId='active';
const fresh={state:'archive',path:'/parent/目标_20001',uid:'20001',history_complete:false,history_cutoff:'2025-09-12'};
Ptu.Api.backupInspect=async req=>{if(req.output_dir!==fresh.path||req.target_uid!=='20001')throw Error('刷新了错误档案');return fresh};
Ptu.setProgress=()=>{};Ptu.setTaskCancelVisible=()=>{};Ptu.renderResult=()=>{};Ptu.toast=()=>{};Ptu._closeTaskWs=()=>{};Ptu.hidePacingStatus=()=>{};
Ptu.refreshSelfArchiveStatus=()=>{};Ptu.refreshHistoryList=()=>{};
Ptu.applyOutputFormatsToCard=()=>{};
(async()=>{
Ptu.handleTaskMessage({type:'done',result:{uid:'20001',output_dir:fresh.path}});
for(let i=0;i<12;i++)await Promise.resolve();
if(Ptu.State.otherArchiveContext.dir!==fresh.path)throw Error('完成后仍然指向建档前的父目录');
if(element('target-backfill-row').hidden)throw Error('首次部分备份后回填入口仍隐藏');
})();
'''))


def test_double_preview_click_creates_only_one_task():
    run_node(app_harness('''
Ptu.State.selfArchiveDir='/book';let calls=0,resolve;
Ptu.toast=()=>{};Ptu.Api.backupPreview=()=>{calls++;return new Promise(r=>resolve=r)};
Ptu.setActiveTask=()=>{};Ptu.setProgress=()=>{};Ptu.subscribeTask=()=>{};
(async()=>{const first=Ptu.startArchivePreview();const second=Ptu.startArchivePreview();
if(calls!==1)throw Error('双击生成预览创建多个任务');
resolve({task_id:'preview'});await Promise.all([first,second]);
if(Ptu.State.taskStartBusy)throw Error('预览启动锁未释放');})();
'''))


def test_late_search_response_does_not_overwrite_new_query_results():
    run_node(app_harness('''
let finishA,finishB,shown;
Ptu.toast=()=>{};Ptu.renderBloggerResults=rows=>{shown=rows};
Ptu.Api.searchUsers=query=>new Promise(r=>{if(query==='甲')finishA=r;else finishB=r});
(async()=>{
element('url-input').value='甲';const a=Ptu.searchOrResolve();
element('url-input').value='乙';const b=Ptu.searchOrResolve();
finishB({results:[{uid:'20002'}]});await b;
finishA({results:[{uid:'20001'}]});await a;
if(shown[0].uid!=='20002')throw Error('旧搜索响应覆盖了新搜索结果');
})();
'''))


def test_error_event_exposes_persisted_recovery_without_restart():
    run_node(app_harness('''
let checked=0;Ptu.State.taskId='active';
Ptu.setProgress=()=>{};Ptu.setTaskCancelVisible=()=>{};Ptu.toast=()=>{};Ptu._closeTaskWs=()=>{};Ptu.hidePacingStatus=()=>{};
Ptu.checkRecoveryTask=()=>{checked++};Ptu.handleTaskMessage({type:'error',error:'暂时失败'});
if(checked!==1)throw Error('错误后没有读取已有恢复点');
'''))


@pytest.mark.parametrize('entry',['library','result'])
def test_native_open_failure_has_visible_feedback(entry):
    run_node(app_harness('''
const messages=[];Ptu.toast=m=>messages.push(m);Ptu.State.isDesktop=true;
window.pywebview={api:{show_in_folder:async()=>false,open_folder:async()=>false}};
Ptu.State._lastResult={output_dir:'/missing'};
(async()=>{
''' + ("await Ptu.openLibraryEntry({path:'/missing'});" if entry=='library' else 'await Ptu.openOutputFolder();') + '''
if(!messages.length)throw Error('打开失败后界面无反馈');})();
'''))


def test_shell_probe_checks_the_runtime_version_not_a_historical_constant():
    from desktop.self_test import shell
    from backend.app.version import VERSION
    assert shell.CURRENT_VERSION == VERSION


def test_late_message_from_previous_task_is_ignored():
    run_node(app_harness('''
global.WsClient=class{on(h){this.handler=h}connect(){}close(){}};
Ptu.maybeShowLogButton=()=>{};Ptu.State.taskId='old';Ptu.subscribeTask('old');
const old=Ptu.State.ws;let delivered=0;Ptu.handleTaskMessage=()=>{delivered++};
Ptu.State.taskId='new';old.handler({type:'done',result:{}});
if(delivered!==0)throw Error('旧任务消息污染新任务');
'''))
