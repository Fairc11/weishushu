"""从已验证的超话通道补充目标作者微博，不遍历普通超话信息流。"""
from __future__ import annotations

from datetime import date
from email.utils import parsedate_to_datetime
import re
from urllib.parse import parse_qs, urlsplit

from weibo_book.errors import OperationPaused, WeiboError, WeiboErrorKind, classify_error

GET_INDEX = 'https://m.weibo.cn/api/container/getIndex'
STATE_KEY = 'supertopic_supplement'


def cards_in(cards):
    if not isinstance(cards,list):
        raise WeiboError('超话卡片清单无效',kind=WeiboErrorKind.PARSE)
    for card in cards:
        if not isinstance(card,dict):
            raise WeiboError('超话卡片无效',kind=WeiboErrorKind.PARSE)
        yield card
        if 'card_group' in card:
            yield from cards_in(card['card_group'])


class SupertopicUnavailable(WeiboError):
    def __init__(self,message):
        super().__init__(message,kind=WeiboErrorKind.API,recoverable=False)


class SupertopicSource:
    def __init__(self,session,uid,scheduler=None):
        self.session=session
        self.uid=str(uid)
        self.scheduler=scheduler

    def get(self,container,cursor=None,extra=None,empty_terminal=False):
        params={'containerid':container}
        if cursor is not None:params['since_id']=cursor
        if extra:params.update(extra)
        def request():
            try:
                response=self.session.get(GET_INDEX,params=params,headers={'Referer':'https://m.weibo.cn/'},timeout=20)
                if response.status_code in {401,403}:
                    raise OperationPaused('登录状态需重新确认，超话检查已暂停',pause_reason='authentication_required')
                if response.status_code in {429,432}:
                    raise OperationPaused('超话请求受到限流，任务已暂停',pause_reason='rate_limited')
                response.raise_for_status()
                payload=response.json()
            except (OperationPaused,WeiboError):raise
            except Exception as exc:
                raise WeiboError('读取超话信息失败',kind=classify_error(exc),original=exc) from exc
            if not isinstance(payload,dict) or not isinstance(payload.get('data'),dict):
                raise WeiboError('超话返回内容无法确认，已保留检查进度',kind=WeiboErrorKind.PARSE)
            if payload.get('ok')!=1:
                # 「TA在超话」翻页到底时服务端返回 ok=0 且空卡片（如“这里还没有内容”），
                # 与无游标末页同义；其余非 1 状态仍视为失败。
                if empty_terminal and payload.get('ok')==0 and payload['data'].get('cards')==[]:
                    return {'cards':[],'cardlistInfo':{}}
                raise WeiboError('超话返回内容无法确认，已保留检查进度',kind=WeiboErrorKind.PARSE)
            return payload['data']
        return self.scheduler.run('profile',request) if self.scheduler is not None else request()

    def discover(self,profile,check_control):
        tabs=profile.get('tabsInfo',{}).get('tabs',[])
        if not isinstance(tabs,list):raise WeiboError('超话入口资料无效',kind=WeiboErrorKind.PARSE)
        topic_tab=next((x for x in tabs if isinstance(x,dict) and x.get('tabKey')=='super_topic'),None)
        if topic_tab is None:return []
        container=topic_tab.get('containerid')
        if not isinstance(container,str) or not container:raise WeiboError('超话入口标识无效',kind=WeiboErrorKind.PARSE)
        check_control()
        topic_page=self.get(container)
        topics={}
        for card in cards_in(topic_page.get('cards',[])):
            if card.get('card_type') not in (101,'101'):continue
            scheme=card.get('scheme');name=card.get('title')
            if not isinstance(scheme,str) or not isinstance(name,str):continue
            url=urlsplit(scheme)
            if url.hostname!='m.weibo.cn' or url.path!='/p/index':continue
            values=parse_qs(url.query).get('containerid',[])
            if len(values)!=1 or not re.fullmatch(r'100808[0-9a-f]{32}',values[0]):continue
            topics[values[0]]=name
        result=[]
        screen_name=(profile.get('userInfo') or {}).get('screen_name')
        for topic,name in topics.items():
            check_control();data=self.get(topic)
            page_info=data.get('pageInfo',{})
            # 优先「TA在超话」全历史来源：服务端按人过滤、覆盖全部年份且翻页终止干净；
            # 找不到该入口时回退名人动态频道（内容深但杂，只作为兜底）。
            inpage=self._discover_inpage(topic,name,screen_name,page_info,check_control)
            if inpage is not None:
                result.append(inpage)
                continue
            heads=page_info.get('cardlist_head_cards',[])
            for head in heads:
                for channel in head.get('channel_list',[]):
                    if channel.get('id')!='live':continue
                    container=channel.get('containerid')
                    if not isinstance(container,str) or not container:raise WeiboError('名人动态入口无效',kind=WeiboErrorKind.PARSE)
                    result.append({'kind':'live','id':topic,'name':name,'containerid':container,'cursor':None,'done':False,'seen_cursors':[]})
        return result

    def _discover_inpage(self,topic,name,screen_name,page_info,check_control):
        """经超话页 search_scheme → 站内搜索 → 「TA在超话」卡片发现全历史来源。

        任何一步缺失都只放弃该来源（保留名人动态），不判定为故障；
        containerid 与 extparam 一律取自服务端返回原值，不自行拼装。
        """
        scheme=page_info.get('search_scheme')
        if not isinstance(screen_name,str) or not screen_name or not isinstance(scheme,str):return None
        parts=urlsplit(scheme)
        if parts.netloc!='searchall':return None
        query=parse_qs(parts.query)
        stype=query.get('type',[]);ext=query.get('extparam',[])
        if len(stype)!=1 or len(ext)!=1:return None
        check_control()
        data=self.get(f'100103type={stype[0]}&q={screen_name}',extra={'extparam':ext[0]})
        for card in cards_in(data.get('cards',[])):
            target=card.get('scheme')
            if not isinstance(target,str) or 'profile_inpage' not in target:continue
            values=parse_qs(urlsplit(target).query)
            cont=values.get('containerid',[]);ep=values.get('extparam',[])
            if len(cont)!=1 or len(ep)!=1:continue
            # 试拉首页验证该来源真实可用；临时失败（限流波动、解析失败等）回退
            # 名人动态兜底，登录失效/限流暂停（OperationPaused）照常向上抛。
            check_control()
            try:
                page=self.get(cont[0],extra={'extparam':ep[0]})
            except WeiboError:
                return None
            # 服务端可能只放出空视图（total 声称有内容，实际一条微博都不返回，
            # 真实案例：某超话 total=78 但全页无微博卡片）；此时视图不可用，
            # 回退名人动态。total 明确为 0 属于博主确实没在超话发过帖，来源有效。
            if not any(isinstance(card.get('mblog'),dict)
                       and str((card['mblog'].get('user') or {}).get('id'))==self.uid
                       for card in cards_in(page.get('cards'))):
                info=page.get('cardlistInfo')
                total=info.get('total') if isinstance(info,dict) else None
                if total!=0:
                    return None
            return {'kind':'profile_inpage','id':topic,'name':name,'containerid':cont[0],
                    'extparam':ep[0],'cursor':None,'done':False,'seen_cursors':[]}
        return None

    def scan(self,state,*,save,progress,check_control,cutoff:date|None=None):
        check_control();profile=self.get('100505'+self.uid)
        user=profile.get('userInfo')
        if not isinstance(user,dict) or str(user.get('id'))!=self.uid:
            raise WeiboError('超话资料账号与归档目标不一致',kind=WeiboErrorKind.AUTH)
        if user.get('verified') is not True:
            raise SupertopicUnavailable('超话补漏仅适用于带认证 V 标识的博主')
        if not state:
            sources=self.discover(profile,check_control)
            if not sources:
                raise SupertopicUnavailable('该认证博主没有可用的超话名人动态入口')
            state.update({'uid':self.uid,'sources':sources,'posts':{},'pages':0})
            save()
        if (state.get('uid')!=self.uid or not isinstance(state.get('sources'),list)
                or not isinstance(state.get('posts'),dict) or type(state.get('pages')) is not int):
            raise WeiboError('超话恢复记录无效',kind=WeiboErrorKind.PARSE,recoverable=False)
        for source in state['sources']:
            if (not isinstance(source,dict) or not all(isinstance(source.get(k),str) for k in ['id','name','containerid'])
                    or type(source.get('done')) is not bool or not isinstance(source.get('seen_cursors'),list)):
                raise WeiboError('超话恢复来源无效',kind=WeiboErrorKind.PARSE,recoverable=False)
            kind=source.get('kind','live')
            if kind not in ('live','profile_inpage') or (kind=='profile_inpage' and not isinstance(source.get('extparam'),str)):
                raise WeiboError('超话恢复来源无效',kind=WeiboErrorKind.PARSE,recoverable=False)
            while not source['done']:
                check_control()
                progress(pages=state['pages'],found=len(state['posts']),name=source['name'])
                cursor=source['cursor']
                if cursor is not None and (isinstance(cursor,bool) or not isinstance(cursor,(str,int))):
                    raise WeiboError('超话翻页标识无效',kind=WeiboErrorKind.PARSE)
                extra={'extparam':source['extparam']} if kind=='profile_inpage' else None
                data=self.get(source['containerid'],cursor,extra,empty_terminal=(kind=='profile_inpage'))
                cards=data.get('cards')
                # 名人动态的翻页游标在 pageInfo，全历史「TA在超话」的游标在 cardlistInfo。
                info=data.get('pageInfo' if kind=='live' else 'cardlistInfo')
                if not isinstance(info,dict):
                    raise WeiboError('超话响应缺少页面信息，不能判定扫描状态',kind=WeiboErrorKind.PARSE)
                new_posts={}
                for card in cards_in(cards):
                    m=card.get('mblog')
                    if not isinstance(m,dict):continue
                    user=m.get('user')
                    if not isinstance(user,dict) or str(user.get('id'))!=self.uid:continue
                    bid=m.get('bid');created=m.get('created_at')
                    if not isinstance(bid,str) or not re.fullmatch(r'[A-Za-z0-9]+',bid) or not isinstance(created,str):
                        raise WeiboError('超话微博标识或日期无效',kind=WeiboErrorKind.PARSE)
                    try:when=parsedate_to_datetime(created)
                    except (TypeError,ValueError) as exc:raise WeiboError('超话微博日期无法解析',kind=WeiboErrorKind.PARSE) from exc
                    if cutoff is not None and when.date()<cutoff:continue
                    synced_now=m.get('sync_mblog') is not False
                    previous=state['posts'].get(bid,new_posts.get(bid))
                    if previous is None:
                        previous={'created_at':when.isoformat(),'topics':[],'synced':synced_now}
                    if not isinstance(previous,dict) or not isinstance(previous.get('topics'),list):
                        raise WeiboError('超话微博恢复记录损坏',kind=WeiboErrorKind.PARSE)
                    record={'created_at':previous['created_at'],'topics':list(previous['topics']),
                            'synced':bool(previous.get('synced',synced_now)) or synced_now}
                    if source['id'] not in record['topics']:record['topics'].append(source['id'])
                    new_posts[bid]=record
                # 实测末页仍有微博卡片，但 pageInfo 不再提供 since_id。
                nxt=info.get('since_id')
                if nxt not in (None,'',0) and (isinstance(nxt,bool) or not isinstance(nxt,(str,int))):
                    raise WeiboError('超话返回的翻页标识无效',kind=WeiboErrorKind.PARSE)
                if nxt not in (None,'',0) and (nxt==cursor or nxt in source['seen_cursors']):
                    raise WeiboError('超话翻页未前进，已停止重复请求',kind=WeiboErrorKind.PARSE)
                state['posts'].update(new_posts)
                state['pages']+=1
                if nxt in (None,'',0):source['done']=True
                else:
                    source['cursor']=nxt;source['seen_cursors'].append(nxt)
                save()
                progress(pages=state['pages'],found=len(state['posts']),name=source['name'])
        return {'posts':state['posts'],'sources':[{'id':x['id'],'name':x['name'],'kind':x.get('kind','live')} for x in state['sources']],
                'complete':all(x['done'] for x in state['sources']),'pages':state['pages']}
