"""入口切换与真实进度的行为回归。"""
from pathlib import Path
from test_frontend_static_contract import run_node

ROOT = Path(__file__).resolve().parents[1]


def source(name):
    text = (ROOT / 'backend/app/static/js/modules' / name).read_text()
    return '\n'.join(line for line in text.splitlines() if not line.startswith('import ')).replace('export function init', 'function init').replace('void initFeedback;', '')


def test_progress_displays_remaining_and_durations_without_invented_total():
    run_node('''
const nodes = {};
globalThis.document = {querySelectorAll: () => []};
globalThis.Ptu = {State: {}, $: id => nodes[id] ||= {textContent:'',style:{},classList:{toggle(){}},querySelector:()=>null}};
''' + source('tasks.js') + '''
Ptu.renderProgressEvent({phase:'media',subphase:'live_replay',current:3,total:8,unit:'segment',elapsed_seconds:10001,phase_elapsed_seconds:65,completed_bytes:1048576,timings_seconds:{media:65,wait:12}});
if (!nodes['progress-stage-count'].textContent.includes('剩余 5')) throw Error('未显示剩余量');
if (!nodes['progress-stage-count'].textContent.includes('段')) throw Error('分段单位丢失');
if (!nodes['progress-stage-elapsed'].textContent.includes('2 小时 46 分 41 秒')) throw Error('长耗时未格式化');
if (!nodes['progress-stage-metrics'].textContent.includes('1.0 MB')) throw Error('下载量未显示');
Ptu.renderProgressEvent({phase:'discover',current:3,total:null,unit:'post'});
if (!nodes['progress-stage-count'].textContent.includes('总量未知')) throw Error('未知总量被杜撰');
''')


def test_tab_switch_keeps_task_and_has_default_library_markup():
    html = (ROOT / 'backend/app/templates/index.html').read_text()
    assert 'data-workspace-tab="library"' in html
    # v2.2.0：工作区页签移入侧边栏块，任务进度卡在主内容区，切换页签不影响任务进度
    sidebar = html[html.index('{% block sidebar_nav %}'):html.index('{% endblock %}')]
    assert 'data-workspace-tab="library"' in sidebar
    assert 'id="step-progress"' not in sidebar
    run_node('''
const nodes={};
globalThis.document={querySelectorAll:()=>[],querySelector:()=>null};
globalThis.Ptu={State:{taskId:'running-task',archiveTarget:null},$: id=>nodes[id] ||= {hidden:false},refreshHistoryList:()=>{},refreshSelfArchiveStatus:()=>{}};
''' + source('archive.js') + '''
Ptu.switchWorkspaceTab('self');
Ptu.switchWorkspaceTab('other');
if(Ptu.State.taskId !== 'running-task') throw Error('切换页签清除了任务');
if(!nodes['self-archive-card'].hidden || nodes['step-2'].hidden) throw Error('页签显示错误');
''')


def test_cancelled_library_start_restores_other_selection():
    run_node('''
const nodes={}; globalThis.document={querySelectorAll:()=>[],querySelector:()=>null};
globalThis.Ptu={State:{loggedIn:true,loginUid:'10001',archiveTarget:{uid:'20001',screen_name:'A'},backupFolderDir:'/A',backupFolderInspection:{state:'archive',path:'/A'}},$:id=>nodes[id]||={classList:{toggle(){}}},guardActiveTask:()=>false,updateTaskActionAvailability:()=>{},toast:()=>{},Api:{backupInspect:async()=>({state:'archive',uid:'30001',path:'/B'})}};
''' + source('archive.js') + '''
Ptu.startArchiveBackup=async()=>{};
(async()=>{await Ptu.updateLibraryEntry({uid:'30001',path:'/B'});
if(Ptu.State.archiveTarget.uid!=='20001'||Ptu.State.backupFolderDir!=='/A')throw Error('书库取消污染了他人选择');})();
''')


def test_saved_output_formats_survive_switching_tabs():
    run_node('''
const nodes={'fmt-pdf':{checked:true},'fmt-markdown':{checked:false}};
globalThis.document={querySelectorAll:()=>[],querySelector:()=>null};
globalThis.Ptu={State:{workspaceTab:'other',archiveTarget:{uid:'20001'},backupFolderInspection:{state:'archive',path:'/A',output_formats:{pdf:false,markdown:false}}},$:id=>nodes[id]||={classList:{toggle(){}}},guardActiveTask:()=>false,toast:()=>{},Api:{backupSettingsSave:async()=>({output_formats:{pdf:true,markdown:false}})}};
''' + source('archive.js') + '''
(async()=>{await Ptu.saveOutputFormats();
if(!Ptu.State.backupFolderInspection.output_formats.pdf)throw Error('保存未更新档案格式');})();
''')


def test_other_tab_restores_selected_archive_after_self_flow():
    run_node('''
const nodes={}; globalThis.document={querySelectorAll:()=>[],querySelector:()=>null};
globalThis.Ptu={State:{workspaceTab:'other'},$:id=>nodes[id]||={classList:{toggle(){}}},guardActiveTask:()=>false,canUpgradeBookFormat:()=>false,currentBookFormat:()=>4};
''' + source('archive.js') + '''
Ptu.selectBloggerTarget({uid:'20001',screen_name:'A'});
Ptu.showPickedTargetInspection({state:'archive',path:'/A',uid:'20001',screen_name:'A',output_formats:{pdf:false,markdown:false}});
Ptu.State.archiveTarget=null; Ptu.State.backupFolderDir='/self';
Ptu.switchWorkspaceTab('other');
if(Ptu.State.archiveTarget.uid!=='20001'||Ptu.State.backupFolderDir!=='/A')throw Error('他人页签没有恢复自己的档案目录');
''')
