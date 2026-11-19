"""备份前一次性检查路由：缓存、取消、身份与不下载约束。"""
import threading

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.routers import router_backup as routes
from backend.app.services.task_manager import task_manager
from tests.test_personal_archive_routes import _wait
from tests.test_video_quality_audit import make_repo, Source
from weibo_book.archive import video_quality as quality


def test_check_route_scans_once_without_downloading(tmp_path, monkeypatch):
    repo=make_repo(tmp_path); root=str(repo._root); repo.close()
    source=Source()
    monkeypatch.setattr(routes,'_http_identity',lambda:{'uid':'10001','screen_name':'本人'})
    monkeypatch.setattr(routes,'build_personal_archive_dependencies',lambda uid,**kw:(source,None))
    with TestClient(app) as client:
        response=client.post('/api/backup/video-quality/check',json={'output_dir':root})
        assert response.status_code==200
        task_id=response.json()['task_id']
        snapshot=_wait(task_id)
        assert snapshot['state']=='done'
        assert snapshot['result']['available_count']==2
        again=client.post('/api/backup/video-quality/check',json={'output_dir':root}).json()
        assert again['task_id'] is None
        assert again['summary']['available_count']==2
        assert source.calls==2


def test_new_verified_archive_needs_no_network_check(tmp_path,monkeypatch):
    repo=make_repo(tmp_path,1);root=str(repo._root)
    quality.remember_verified(repo,repo.list_media_for_render()[0]);repo.close()
    monkeypatch.setattr(routes,'_http_identity',lambda:{'uid':'10001','screen_name':'本人'})
    def forbidden(*args,**kwargs):raise AssertionError('不应重新联网检查')
    monkeypatch.setattr(routes,'build_personal_archive_dependencies',forbidden)
    with TestClient(app) as client:
        response=client.post('/api/backup/video-quality/check',json={'output_dir':root})
        assert response.status_code==200
        assert response.json()['task_id'] is None


def test_check_cancellation_waits_for_safe_exit(tmp_path,monkeypatch):
    repo=make_repo(tmp_path,1);root=str(repo._root);repo.close()
    entered=threading.Event();release=threading.Event()
    class Blocking(Source):
        def fetch_video_playback_url(self,bid):
            entered.set();release.wait(3)
            return super().fetch_video_playback_url(bid)
    monkeypatch.setattr(routes,'_http_identity',lambda:{'uid':'10001','screen_name':'本人'})
    monkeypatch.setattr(routes,'build_personal_archive_dependencies',lambda uid,**kw:(Blocking(),None))
    with TestClient(app) as client:
        response=client.post('/api/backup/video-quality/check',json={'output_dir':root})
        assert response.status_code==200
        task_id=response.json()['task_id'];assert entered.wait(2)
        assert client.post(f'/api/tasks/{task_id}/cancel').json()['cancelled']
        release.set()
        assert _wait(task_id)['state']=='cancelled'
