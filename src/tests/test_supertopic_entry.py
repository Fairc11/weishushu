import pytest
from test_focused_workflow import app_harness,run_node


@pytest.mark.parametrize('verified,visible',[(True,True),(False,False),(None,False)])
def test_supertopic_entry_requires_verified_blogger_and_existing_archive(verified,visible):
    import json
    run_node(app_harness('''
Ptu.State.workspaceTab='other';Ptu.applyOutputFormatsToCard=()=>{};
''' + f"Ptu.selectBloggerTarget({{uid:'20001',screen_name:'测试',verified:{json.dumps(verified)}}});" + '''
Ptu.showPickedTargetInspection({state:'archive',path:'/book',uid:'20001',history_complete:true});
''' + f"if(element('target-supertopic-row').hidden!=={str(not visible).lower()})throw Error('认证入口显示错误');" + '''
Ptu.resetPickedTargetFolder();
if(!element('target-supertopic-row').hidden)throw Error('换目录后遗留了入口');
'''))


def test_supertopic_start_uses_saved_target_without_video_upgrade_prompt():
    run_node(app_harness('''
Ptu.State.loggedIn=true;
Ptu.State.archiveTarget={uid:'20001',screen_name:'测试',verified:true};
Ptu.State.backupFolderDir='/book';Ptu.State.backupFolderInspection={state:'archive',path:'/book',uid:'20001'};
let request;Ptu.toast=()=>{};Ptu.setActiveTask=()=>{};Ptu.setProgress=()=>{};Ptu.subscribeTask=()=>{};Ptu.setTaskCancelVisible=()=>{};Ptu.closeArchiveFolderDecision=()=>{};
Ptu.Api.backupStart=async req=>{request=req;return {task_id:'x',self_screen_name:'测试'}};
Ptu.Api.backupVideoQualityCheck=async()=>{throw Error('不得附加旧视频升级')};
(async()=>{await Ptu.startSupertopicSupplement();
if(!request||!request.include_supertopics||request.target_uid!=='20001'||request.output_dir!=='/book')throw Error('超话补漏请求错误');})();
'''))
