from copy import deepcopy
from datetime import date
import pytest
from weibo_book.errors import WeiboError, OperationPaused

UID='20001'
TOPIC='100808'+'a'*32
CURSOR='{"page":2,"nextscore":123.500000,"msgid":999}'


def payload(cards, cursor=None):
    return {'ok':1,'data':{'cards':cards,'pageInfo':{'since_id':cursor}}}


def post(bid,uid=UID,when='Mon Sep 07 09:34:37 +0800 2026'):
    return {'card_type':'9','mblog':{'bid':bid,'id':bid,'user':{'id':uid},'created_at':when,'sync_mblog':False}}


class Response:
    status_code=200
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return deepcopy(self.data)


class Session:
    def __init__(self,verified=True):self.calls=[];self.verified=verified;self.fail=False
    def get(self,url,params,**kw):
        self.calls.append(dict(params));c=params['containerid']
        if c=='100505'+UID:
            return Response({'ok':1,'data':{'userInfo':{'id':UID,'verified':self.verified},'tabsInfo':{'tabs':[{'tabKey':'super_topic','containerid':'author-topics'}]}}})
        if c=='author-topics':return Response({'ok':1,'data':{'cards':[{'card_type':101,'title':'合成超话','scheme':'https://m.weibo.cn/p/index?containerid='+TOPIC}]}})
        if c==TOPIC:return Response({'ok':1,'data':{'pageInfo':{'cardlist_head_cards':[{'channel_list':[{'id':'live','containerid':TOPIC+'_-_live'}]}]}}})
        if params.get('since_id')==CURSOR:
            if self.fail:raise OperationPaused('暂停')
            return Response(payload([post('A'),post('B')],''))
        return Response(payload([{'card_group':[post('A'),post('FAN','90001')]}],CURSOR))


def scan(session,state=None,cutoff=None):
    from weibo_book.archive.supertopic import SupertopicSource
    state={} if state is None else state
    return SupertopicSource(session, UID).scan(state,save=lambda:None,progress=lambda **kw:None,check_control=lambda:None,cutoff=cutoff)


@pytest.mark.parametrize('verified',[False,None,'true',1])
def test_only_actual_verified_true_can_use_topic_source(verified):
    session=Session(verified)
    with pytest.raises(WeiboError,match='认证'):
        scan(session)
    assert len(session.calls)==1


def test_topic_source_filters_author_deduplicates_and_keeps_cursor_exact():
    session=Session();result=scan(session)
    assert set(result['posts'])=={'A','B'}
    assert result['posts']['A']['topics']==[TOPIC]
    assert result['complete'] is True
    assert session.calls[-1]['since_id']==CURSOR


def test_paused_scan_resumes_from_saved_cursor_without_losing_ids():
    session=Session();session.fail=True;state={}
    with pytest.raises(OperationPaused):scan(session,state)
    assert 'A' in state['posts']
    session.fail=False;session.calls.clear()
    result=scan(session,state)
    assert set(result['posts'])=={'A','B'}
    feeds=[x for x in session.calls if x['containerid']==TOPIC+'_-_live']
    assert len(feeds)==1 and feeds[0]['since_id']==CURSOR


def test_date_filter_does_not_stop_activity_pagination():
    session=Session();original=session.get
    def get(url,params,**kw):
        if params['containerid']==TOPIC+'_-_live' and not params.get('since_id'):
            session.calls.append(dict(params));return Response(payload([post('OLD',when='Mon Sep 07 09:34:37 +0800 2020')],CURSOR))
        return original(url,params,**kw)
    session.get=get
    result=scan(session,cutoff=date(2026,1,1))
    assert set(result['posts'])=={'A','B'}


def test_sync_supplement_adds_only_missing_posts_and_keeps_profile_history(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    from test_staged_backup import post
    from dataclasses import replace
    class IdentityProvider:
        def whoami(self):return {'uid':UID,'screen_name':'测试用户'}
    def make_post(bid):return replace(post(bid,date(2026,9,7)),uid=UID)
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试用户')
    repo.upsert_post(post_to_record(make_post('A')))
    repo.write_history_complete(True);repo.close()
    session=Session()
    class Source:
        def supertopic_source(self,uid):
            assert uid==UID
            return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):
            assert bid=='B';return make_post(bid)
        def fetch_recent_comments(self,*a,**kw):return []
    sync=PersonalArchiveSync(tmp_path/'book',Source(),IdentityProvider(),include_supertopics=True)
    result=sync.run('incremental')
    assert result.new_posts==1 and result.refreshed_posts==0
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    assert repo.list_known_bids()=={'A','B'}
    assert repo.read_history_complete() is True
    meta=repo._read_meta(STATE_KEY)
    assert meta['post_sources']['B']==[TOPIC]
    assert meta['exclusive_bids']==['B']
    repo.close()


def test_snapshot_keeps_topic_origin_without_changing_original_source(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.schema import PostRecord
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot
    from weibo_book.archive.supertopic import STATE_KEY
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(PostRecord('A',UID,'正文',source='iPhone'))
    repo._write_meta(STATE_KEY,{'sources':[{'id':TOPIC,'name':'合成超话'}],'post_sources':{'A':[TOPIC]}})
    value=ArchiveRenderSnapshot.from_repository(repo).posts[0]
    assert value['source']=='iPhone'
    assert value['supertopics'][0]['name']=='合成超话'
    repo.close()


def test_verified_terminal_page_without_cursor_keeps_its_last_posts():
    session=Session();original=session.get
    def get(url,params,**kw):
        if params.get('since_id')==CURSOR:
            return Response({'ok':1,'data':{'pageInfo':{'containerid':TOPIC+'_-_live'},'cards':[post('LAST')]}})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert set(result['posts'])=={'A','LAST'} and result['complete']


def test_removed_verification_disables_automatic_supplement_but_keeps_main_backup(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.discovery import ProfilePage
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    session=Session(False)
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo._write_meta(STATE_KEY,{'enabled':True,'post_sources':{}});repo.close()
    sync=PersonalArchiveSync(tmp_path/'book',Source(),Identity())
    sync.run('incremental')
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    assert repo._read_meta(STATE_KEY)['enabled'] is False
    assert sync.supertopic_result['status']=='unavailable'
    assert len(session.calls)==1
    repo.close()


def test_snapshot_marks_exclusive_posts_and_excludes_them_from_main_timeline(tmp_path):
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.schema import PostRecord
    from weibo_book.archive.render_snapshot import ArchiveRenderSnapshot
    from weibo_book.archive.supertopic import STATE_KEY
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(PostRecord('MAIN',UID,'主页微博',created_at='2026-09-01T00:00:00+00:00'))
    repo.upsert_post(PostRecord('ONLY',UID,'超话微博',created_at='2026-09-02T00:00:00+00:00'))
    repo._write_meta(STATE_KEY,{'sources':[{'id':TOPIC,'name':'合成超话'}],
                                'post_sources':{'ONLY':[TOPIC]},'exclusive_bids':['ONLY']})
    snapshot=ArchiveRenderSnapshot.from_repository(repo)
    by_bid={post['bid']:post for post in snapshot.posts}
    assert by_bid['ONLY']['supertopic_exclusive'] is True
    assert 'supertopic_exclusive' not in by_bid['MAIN']
    assert snapshot.timeline['normal_count']==1
    repo.close()


def test_exclusive_mark_cleared_when_post_appears_on_profile(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.discovery import ProfileItem,ProfilePage
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    from test_staged_backup import post
    from dataclasses import replace
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    def make_post(bid):return replace(post(bid,date(2026,9,7)),uid=UID)
    session=Session()
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([ProfileItem('B')],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):return make_post(bid)
        def fetch_recent_comments(self,*a,**kw):return []
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(post_to_record(make_post('B')))
    repo.write_history_complete(True)
    repo._write_meta(STATE_KEY,{'enabled':True,'sources':[{'id':TOPIC,'name':'合成超话'}],
                                'post_sources':{'B':[TOPIC]},'exclusive_bids':['B']})
    repo.close()
    PersonalArchiveSync(tmp_path/'book',Source(),Identity()).run('incremental')
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    # 超话扫描同时补入新微博 A；已在主页出现的 B 解除独占
    assert meta['exclusive_bids']==['A']
    assert meta['post_sources']['B']==[TOPIC]
    repo.close()


def test_supplement_added_post_synced_to_profile_stays_in_main_feed(tmp_path):
    """sync_mblog=true 的补漏新增微博同步在主页，不标独占；sync_mblog=false 才独占。"""
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    from test_staged_backup import post as make_std_post
    from dataclasses import replace
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    def make_post(bid):return replace(make_std_post(bid,date(2026,9,7)),uid=UID)
    synced=dict(post('S'));synced['mblog']=dict(synced['mblog']);synced['mblog']['sync_mblog']=True
    class SyncedSession(Session):
        def get(self,url,params,**kw):
            if params['containerid']==TOPIC+'_-_live' and params.get('since_id')==CURSOR:
                return Response(payload([post('A'),post('B'),synced],''))
            return super().get(url,params,**kw)
    session=SyncedSession()
    class Source:
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):return make_post(bid)
        def fetch_recent_comments(self,*a,**kw):return []
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(post_to_record(make_post('A')))
    repo.write_history_complete(True);repo.close()
    sync=PersonalArchiveSync(tmp_path/'book',Source(),Identity(),include_supertopics=True)
    result=sync.run('incremental')
    assert result.new_posts==2
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    assert meta['exclusive_bids']==['B']
    assert meta['post_sources']['S']==[TOPIC]
    repo.close()


def test_rescan_unmarks_exclusive_when_post_actually_synced(tmp_path):
    """重扫时 sync_mblog=true 的旧独占标记解除（修正误判），未扫到的保留。"""
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    from test_staged_backup import post as make_std_post
    from dataclasses import replace
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    def make_post(bid):return replace(make_std_post(bid,date(2026,9,7)),uid=UID)
    session=Session()
    from weibo_book.archive.discovery import ProfilePage
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):return make_post(bid)
        def fetch_recent_comments(self,*a,**kw):return []
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    for bid in ['A','B','OLDX']:
        repo.upsert_post(post_to_record(make_post(bid)))
    repo.write_history_complete(True)
    repo._write_meta(STATE_KEY,{'enabled':True,'sources':[{'id':TOPIC,'name':'合成超话'}],
                                'post_sources':{'A':[TOPIC],'B':[TOPIC],'OLDX':[TOPIC]},
                                'exclusive_bids':['A','B','OLDX']})
    repo.close()
    # 本轮扫描中 A 的 sync_mblog 变为 true（实际同步在主页），B 仍 false；OLDX 不在扫描范围
    synced=dict(post('A'));synced['mblog']=dict(synced['mblog']);synced['mblog']['sync_mblog']=True
    original=session.get
    def get(url,params,**kw):
        if params['containerid']==TOPIC+'_-_live' and params.get('since_id')==CURSOR:
            return Response(payload([synced,post('B')],''))
        return original(url,params,**kw)
    session.get=get
    PersonalArchiveSync(tmp_path/'book',Source(),Identity()).run('incremental')
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    assert meta['exclusive_bids']==['B','OLDX']
    repo.close()


def _make_sync_source(session, fetched=()):
    from weibo_book.archive.discovery import ProfilePage
    from weibo_book.archive.supertopic import SupertopicSource
    from test_staged_backup import post as staged_post
    from dataclasses import replace
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):
            assert bid in fetched
            return replace(staged_post(bid,date(2026,9,7)),uid=uid)
        def fetch_recent_comments(self,*a,**kw):return []
    return Source()


class _Identity:
    def whoami(self):return {'uid':UID,'screen_name':'测试'}


def test_incremental_auto_discovers_supertopics_without_manual_enable(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.supertopic import STATE_KEY
    from test_staged_backup import post as staged_post
    from dataclasses import replace
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(post_to_record(replace(staged_post('A',date(2026,9,7)),uid=UID)))
    repo.write_history_complete(True);repo.close()
    session=Session()
    sync=PersonalArchiveSync(tmp_path/'book',_make_sync_source(session,fetched=('B',)),_Identity())
    result=sync.run('incremental')
    assert result.new_posts==1
    assert sync.supertopic_result['found']==2 and sync.supertopic_result['new']==1
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    assert meta['enabled'] is True and meta['post_sources']['B']==[TOPIC]
    assert repo.list_known_bids()=={'A','B'}
    repo.close()


def test_auto_supplement_unavailable_is_silent_and_cooled_down(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.supertopic import STATE_KEY
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试');repo.close()
    session=Session(False)
    source=_make_sync_source(session)
    sync=PersonalArchiveSync(tmp_path/'book',source,_Identity())
    sync.run('incremental')
    assert sync.supertopic_result=={}
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    assert meta['enabled'] is False and meta['status']=='unavailable' and meta['checked_at']
    repo.close()
    first_calls=len(session.calls)
    assert first_calls==1
    again=PersonalArchiveSync(tmp_path/'book',source,_Identity())
    again.run('incremental')
    assert len(session.calls)==first_calls
    assert again.supertopic_result=={}


def test_create_range_limits_auto_supplement(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync
    from weibo_book.archive.repository import ArchiveRepository
    session=Session();original=session.get
    def get(url,params,**kw):
        if params['containerid']==TOPIC+'_-_live' and not params.get('since_id'):
            session.calls.append(dict(params))
            return Response(payload([post('OLD',when='Mon Sep 07 09:34:37 +0800 2020')],CURSOR))
        return original(url,params,**kw)
    session.get=get
    sync=PersonalArchiveSync(tmp_path/'book',_make_sync_source(session,fetched=('A','B')),_Identity(),range_months=12)
    sync.run('create')
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    assert repo.list_known_bids()=={'A','B'}
    repo.close()


INPAGE='231140'+'a'*32+'_-_profile_inpage'
CURSOR2='2_5334095827569014'
EP='lfid%23100103type%3D532%7Ctarget_uid%2320001%7Ctopicid%231022%3A'+TOPIC
SEARCH_EXT='{"topicid":"1022:'+TOPIC+'","pagetype":"index"}'


class DeepSession(Session):
    """带「TA在超话」全历史入口的会话：超话页提供 search_scheme，
    站内搜索返回 profile_inpage 卡片，深历史页经 cardlistInfo.since_id 翻页。"""

    def get(self,url,params,**kw):
        from urllib.parse import quote
        c=params['containerid']
        if c=='100505'+UID:
            self.calls.append(dict(params))
            return Response({'ok':1,'data':{'userInfo':{'id':UID,'verified':self.verified,'screen_name':'测试博主'},'tabsInfo':{'tabs':[{'tabKey':'super_topic','containerid':'author-topics'}]}}})
        if c==TOPIC:
            self.calls.append(dict(params))
            return Response({'ok':1,'data':{'pageInfo':{
                'cardlist_head_cards':[{'channel_list':[{'id':'live','containerid':TOPIC+'_-_live'}]}],
                'search_scheme':'sinaweibo://searchall?type=532&extparam='+quote(SEARCH_EXT)}}})
        if c=='100103type=532&q=测试博主':
            self.calls.append(dict(params))
            assert params.get('extparam')==SEARCH_EXT
            return Response({'ok':1,'data':{'cards':[{'card_type':11,'card_group':[
                {'card_type':10,'scheme':'https://m.weibo.cn/p/index?containerid='+INPAGE+'&extparam='+quote(EP)}]}]}})
        if c==INPAGE:
            self.calls.append(dict(params))
            assert params.get('extparam')==EP
            if params.get('since_id')==CURSOR2:
                return Response({'ok':1,'data':{'cards':[post('OLD',when='Tue Apr 18 17:08:19 +0800 2017')],'cardlistInfo':{'since_id':None}}})
            return Response({'ok':1,'data':{'cards':[post('C'),post('A')],'cardlistInfo':{'since_id':CURSOR2}}})
        return super().get(url,params,**kw)


def test_profile_inpage_preferred_over_shallow_live_channel():
    session=DeepSession();result=scan(session)
    assert set(result['posts'])=={'A','C','OLD'}
    assert result['posts']['A']['topics']==[TOPIC]
    assert result['complete'] is True
    assert [x['kind'] for x in result['sources']]==['profile_inpage']
    assert not [x for x in session.calls if x['containerid']==TOPIC+'_-_live']
    inpage=[x for x in session.calls if x['containerid']==INPAGE]
    # 发现阶段试拉首页验证可用 + 正式扫描两页
    assert len(inpage)==3 and inpage[-1]['since_id']==CURSOR2
    assert all(x['extparam']==EP for x in inpage)


def test_profile_inpage_respects_date_cutoff_but_keeps_paging():
    session=DeepSession();result=scan(session,cutoff=date(2026,1,1))
    assert set(result['posts'])=={'A','C'}


def test_search_without_inpage_card_keeps_live_source_only():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']=='100103type=532&q=测试博主':
            session.calls.append(dict(params))
            return Response({'ok':1,'data':{'cards':[{'card_type':11,'card_group':[post('X')]}]}})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert set(result['posts'])=={'A','B'}


def test_legacy_live_only_saved_state_still_validates():
    session=Session()
    state={'uid':UID,'sources':[{'id':TOPIC,'name':'合成超话','containerid':TOPIC+'_-_live',
                                 'cursor':None,'done':False,'seen_cursors':[]}],
           'posts':{},'pages':0}
    result=scan(session,state)
    assert set(result['posts'])=={'A','B'}


def test_authoritative_inpage_scan_replaces_wrong_live_attributions(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync,post_to_record
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.discovery import ProfilePage
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    from test_staged_backup import post
    from dataclasses import replace
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    def make_post(bid):return replace(post(bid,date(2026,9,7)),uid=UID)
    session=DeepSession()
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):return make_post(bid)
        def fetch_recent_comments(self,*a,**kw):return []
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.upsert_post(post_to_record(make_post('WRONG')))
    repo.upsert_post(post_to_record(make_post('C')))
    repo.write_history_complete(True)
    repo._write_meta(STATE_KEY,{'enabled':True,'sources':[{'id':TOPIC,'name':'合成超话'}],
                                'post_sources':{'WRONG':[TOPIC],'C':[TOPIC]},'exclusive_bids':[]})
    repo.close()
    PersonalArchiveSync(tmp_path/'book',Source(),Identity()).run('incremental')
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    origins=repo._read_meta(STATE_KEY)['post_sources']
    # 重扫后 WRONG 不在「TA在超话」结果中，错误标注自动解除；C 保留
    assert 'WRONG' not in origins
    assert origins['C']==[TOPIC]
    repo.close()


def test_inpage_trial_fetch_failure_falls_back_to_live():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']==INPAGE:
            session.calls.append(dict(params))
            return Response({'ok':0,'msg':'临时异常'})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert set(result['posts'])=={'A','B'}
    assert [x['kind'] for x in result['sources']]==['live']
    assert not [x for x in session.calls if x['containerid']==INPAGE and x.get('since_id')]


def test_auto_supplement_scan_error_does_not_abort_main_backup(tmp_path):
    from weibo_book.archive.sync import PersonalArchiveSync
    from weibo_book.archive.repository import ArchiveRepository
    from weibo_book.archive.discovery import ProfileItem,ProfilePage
    from weibo_book.archive.supertopic import SupertopicSource,STATE_KEY
    class Identity:
        def whoami(self):return {'uid':UID,'screen_name':'测试'}
    session=Session();original=session.get
    def get(url,params,**kw):
        if params['containerid']==TOPIC+'_-_live':
            session.calls.append(dict(params))
            return Response({'ok':-1,'msg':'临时异常'})
        return original(url,params,**kw)
    session.get=get
    class Source:
        def iter_profile_pages(self,uid):yield ProfilePage([ProfileItem('NEW1')],True)
        def supertopic_source(self,uid):return SupertopicSource(session,uid)
        def fetch_post(self,uid,bid):
            from test_staged_backup import post
            from dataclasses import replace
            return replace(post(bid,date(2026,9,7)),uid=UID)
        def fetch_recent_comments(self,*a,**kw):return []
    repo=ArchiveRepository.create(tmp_path/'book',UID,'测试')
    repo.write_history_complete(True)
    repo._write_meta(STATE_KEY,{'enabled':True,'sources':[{'id':TOPIC,'name':'合成超话'}],
                                'post_sources':{'KEEP':[TOPIC]},'exclusive_bids':[]})
    repo.close()
    sync=PersonalArchiveSync(tmp_path/'book',Source(),Identity())
    result=sync.run('incremental')
    assert result.new_posts==1
    assert sync.supertopic_result['status']=='error'
    repo=ArchiveRepository.open(tmp_path/'book',UID)
    meta=repo._read_meta(STATE_KEY)
    assert meta['enabled'] is True and meta['post_sources']=={'KEEP':[TOPIC]}
    repo.close()
    manual=PersonalArchiveSync(tmp_path/'book',Source(),Identity(),include_supertopics=True)
    with pytest.raises(WeiboError,match='超话返回内容无法确认'):
        manual.run('incremental')


def test_inpage_empty_terminal_page_keeps_first_page_posts():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']==INPAGE and params.get('since_id')==CURSOR2:
            session.calls.append(dict(params))
            return Response({'ok':0,'msg':'这里还没有内容','data':{'cards':[]}})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert set(result['posts'])=={'A','C'}
    assert result['complete'] is True


def test_inpage_error_payload_without_cards_still_raises():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']==INPAGE and params.get('since_id')==CURSOR2:
            session.calls.append(dict(params))
            return Response({'ok':-1,'msg':'系统繁忙','data':{}})
        return original(url,params,**kw)
    session.get=get
    with pytest.raises(WeiboError,match='超话返回内容无法确认'):
        scan(session)


def test_inpage_empty_view_with_nonzero_total_falls_back_to_live():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']==INPAGE and not params.get('since_id'):
            session.calls.append(dict(params))
            return Response({'ok':1,'data':{'cards':[{'card_type':11,'card_group':[{'card_type':10}]}],'cardlistInfo':{'total':78,'since_id':None}}})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert set(result['posts'])=={'A','B'}
    assert [x['kind'] for x in result['sources']]==['live']


def test_inpage_legitimately_empty_view_is_accepted():
    session=DeepSession();original=session.get
    def get(url,params,**kw):
        if params['containerid']==INPAGE:
            session.calls.append(dict(params))
            return Response({'ok':1,'data':{'cards':[{'card_type':11,'card_group':[{'card_type':10}]}],'cardlistInfo':{'total':0,'since_id':None}}})
        return original(url,params,**kw)
    session.get=get
    result=scan(session)
    assert result['posts']=={}
    assert result['complete'] is True
    assert [x['kind'] for x in result['sources']]==['profile_inpage']
