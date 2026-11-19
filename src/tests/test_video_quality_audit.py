"""旧视频一次性核验、队列选择与新视频标记。"""
from dataclasses import replace

import pytest

from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import MediaRecord, PostRecord
from weibo_book.errors import WeiboError


def make_repo(tmp_path, count=2):
    repo=ArchiveRepository.create(tmp_path/'archive',uid='10001',screen_name='测试')
    for n in range(count):
        bid=f'V{n:03d}'
        repo.upsert_post(PostRecord(bid=bid,uid='10001',text='测试视频',created_at=f'2026-01-{n%28+1:02d}',media_signature=[{'type':'video','position':0}]))
        repo.upsert_media(MediaRecord('post',bid,'video',0,
            f'https://v.test/{bid}.mp4?label=mp4_720p&template=1280x720',
            f'media/{bid}.mp4',f'{n+1:064x}'))
    return repo


class Source:
    calls=0
    def fetch_video_playback_url(self,bid):
        self.calls+=1
        return f'https://v.test/{bid}.mp4?label=mp4_1080p&template=1920x1080'


def test_legacy_scan_cached_and_pending_list_is_not_rescanned(tmp_path):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path); source=Source()
    assert quality.summary(repo)['unchecked_count']==2
    assert quality.scan(repo,source)['available_count']==2
    quality.scan(repo,source)
    assert source.calls==2
    assert quality.summary(repo)['unchecked_count']==0
    assert all('/V' in m.local_path for m in repo.list_media_for_render())
    repo.close()


def test_new_verified_media_and_signed_url_refresh_do_not_need_scan(tmp_path):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path,1); record=repo.list_media_for_render()[0]
    quality.remember_verified(repo,record)
    repo.upsert_media(replace(record,remote_url=record.remote_url+'&Expires=2'))
    source=Source(); quality.scan(repo,source)
    assert source.calls==0
    assert quality.summary(repo)['available_count']==0
    repo.close()


def test_failed_check_is_retryable_and_max_available_is_not(tmp_path):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path,1)
    class Broken(Source):
        def fetch_video_playback_url(self,bid):raise WeiboError('网络暂时失败')
    assert quality.scan(repo,Broken())['failed_count']==1
    class Maximum(Source):
        def fetch_video_playback_url(self,bid):
            self.calls+=1
            return repo.list_media_for_render()[0].remote_url
    source=Maximum(); quality.scan(repo,source); quality.scan(repo,source)
    assert source.calls==1
    assert quality.summary(repo)['failed_count']==0
    repo.close()


@pytest.mark.parametrize('limit,expected',[(0,0),(10,10),(50,50),('all',55)])
def test_selection_limit_and_resume_does_not_expand_scope(tmp_path,limit,expected):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path,55);source=Source();quality.scan(repo,source)
    downloads=[]
    def download(paths,bid,position,url,year_month):
        downloads.append(bid)
        return f'media/{bid}-high.mp4','f'*64
    count=quality.apply_selected(repo,source,'run-one',limit,download)
    assert count==expected
    quality.apply_selected(repo,source,'run-one',limit,download)
    assert len(downloads)==expected
    assert quality.summary(repo)['available_count']==55-expected
    repo.close()


def test_failure_preserves_file_and_queue(tmp_path):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path,1);source=Source();quality.scan(repo,source)
    old=repo.list_media_for_render()[0]
    def download(*args):raise WeiboError('下载暂时失败')
    assert quality.apply_selected(repo,source,'run',10,download)==0
    assert repo.list_media_for_render()[0]==old
    assert quality.summary(repo)['available_count']==1
    repo.close()


def test_backup_request_has_explicit_strict_upgrade_choice():
    from backend.app.schemas import PersonalArchiveRequest
    from pydantic import ValidationError
    base=dict(output_dir='/tmp/test',mode='incremental',pacing_mode='standard',keep_awake_when_plugged=False)
    assert PersonalArchiveRequest(**base).video_upgrade_limit==0
    for value in [0,10,50,'all']:
        assert PersonalArchiveRequest(**base,video_upgrade_limit=value).video_upgrade_limit==value
    for value in [True,False,-1,1,11,'10',None]:
        with pytest.raises(ValidationError):PersonalArchiveRequest(**base,video_upgrade_limit=value)


def test_same_verified_content_keeps_marker_when_local_path_changes(tmp_path):
    from weibo_book.archive import video_quality as quality
    repo=make_repo(tmp_path,1);old=repo.list_media_for_render()[0]
    quality.remember_verified(repo,old)
    repo.upsert_media(replace(old,local_path='media/moved.mp4'))
    source=Source();quality.scan(repo,source)
    assert source.calls==0
    repo.close()


@pytest.mark.parametrize('verified',[True,False])
def test_normal_new_download_records_successful_verification(tmp_path,verified):
    import hashlib
    from datetime import datetime,timezone
    from weibo_book.archive import video_quality as quality
    from weibo_book.archive.sync import PersonalArchiveSync,StagedMedia
    from weibo_book.archive.discovery import ProfileItem,ProfilePage
    from weibo_book.models import Post,PostMedia,MediaType
    high='https://v.test/new.mp4?label=mp4_1080p&template=1920x1080'
    class Identity:
        def whoami(self):return {'uid':'10001','screen_name':'测试'}
    class NewSource:
        def iter_profile_pages(self,uid):yield ProfilePage([ProfileItem('NEW')],is_last=True)
        def fetch_post(self,uid,bid):return Post(bid=bid,uid=uid,user_name='测试',user_avatar='',text='视频',created_at=datetime(2026,1,1,tzinfo=timezone.utc),media=[PostMedia(type=MediaType.VIDEO,url=high)])
        def fetch_recent_comments(self,*args,**kwargs):return []
        def verified_video_url(self,bid):return high if verified else None
    class Stager:
        def stage(self,post,comments,work_root,**kwargs):
            path=work_root/'new.mp4';path.write_bytes(b'video')
            return [StagedMedia(MediaRecord('post','NEW','video',0,high,'media/new.mp4',hashlib.sha256(b'video').hexdigest()),path)]
    root=tmp_path/'archive'
    PersonalArchiveSync(root,NewSource(),Identity(),media_stager=Stager()).run('create')
    repo=ArchiveRepository.open(root,'10001')
    assert quality.summary(repo)['unchecked_count']==(0 if verified else 1)
    repo.close()


def test_cancelled_selection_resumes_only_original_scope(tmp_path):
    from weibo_book.archive import video_quality as quality
    from weibo_book.errors import OperationCancelled
    repo=make_repo(tmp_path,15);source=Source();quality.scan(repo,source)
    completed=[]
    def partial(paths,bid,position,url,year_month):
        if len(completed)==5:raise OperationCancelled('暂停测试')
        completed.append(bid)
        return f'media/{bid}-new.mp4','f'*64
    with pytest.raises(OperationCancelled):quality.apply_selected(repo,source,'same-run',10,partial)
    def finish(paths,bid,position,url,year_month):
        completed.append(bid)
        return f'media/{bid}-new.mp4','f'*64
    quality.apply_selected(repo,source,'same-run',10,finish)
    assert len(completed)==10
    assert len(set(completed))==10
    assert quality.summary(repo)['available_count']==5
    repo.close()


def test_corrupt_selection_does_not_silently_expand_scope(tmp_path):
    from weibo_book.archive import video_quality as quality
    from weibo_book.archive.repository import ArchiveError
    repo=make_repo(tmp_path,15);source=Source();quality.scan(repo,source)
    repo._write_meta('video_quality_selection',123)
    with pytest.raises(ArchiveError):quality.apply_selected(repo,source,'same-run',10,lambda *args:None)
    repo.close()
