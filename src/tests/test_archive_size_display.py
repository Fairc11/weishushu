"""书库条目与备份完成页的档案占用显示。"""
import os
import re

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from backend.app.main import app
from backend.app.services import archive_library as library
from test_backup_flow_ui import load_page, stub_common
from tests.frontend_assets import css_bundle_asset, frontend_bundle_asset  # noqa: F401  (保持与 UI 测试同源的资产聚合)
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.errors import WeiboError


def _archive(path, uid='10001'):
    repo = ArchiveRepository.create(path, uid=uid, screen_name='合成用户')
    repo.close()
    return str(path)


def test_archive_size_counts_all_files_and_refreshes_after_db_change(tmp_path, monkeypatch):
    monkeypatch.setattr(library, '_registry_path', lambda: tmp_path / 'state' / 'archive-library.json')
    root = _archive(tmp_path / 'book')
    media = tmp_path / 'book' / '数据与媒体' / '媒体' / '微博图片'
    media.mkdir(parents=True)
    (media / 'a.jpg').write_bytes(b'x' * 100)
    first = library.archive_size_bytes(root)
    assert first >= 100
    (media / 'b.jpg').write_bytes(b'y' * 200)
    database = tmp_path / 'book' / '数据与媒体' / '数据' / 'archive.db'
    os.utime(database)
    assert library.archive_size_bytes(root) == first + 200


def test_archive_size_rejects_relative_and_missing_paths(tmp_path):
    with pytest.raises(WeiboError):
        library.archive_size_bytes('relative/path')
    with pytest.raises(WeiboError):
        library.archive_size_bytes(str(tmp_path / 'missing'))


def test_registered_sizes_cover_all_rows_and_missing_dir_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(library, '_registry_path', lambda: tmp_path / 'state' / 'archive-library.json')
    first = _archive(tmp_path / 'first')
    second = _archive(tmp_path / 'second')
    library.register_archive(first, expected_uid='10001')
    library.register_archive(second, expected_uid='10001')
    rows = library.registered_archive_sizes()
    assert {row['path'] for row in rows} == {first, second}
    assert all(row['size_bytes'] > 0 for row in rows)
    (tmp_path / 'second').rename(tmp_path / 'moved-away')
    rows = library.registered_archive_sizes()
    assert next(row for row in rows if row['path'] == second)['size_bytes'] is None


def test_sizes_route_validates_path_and_returns_single_size(tmp_path):
    root = _archive(tmp_path / 'book')
    with TestClient(app) as client:
        bad = client.get('/api/backup/library/sizes', params={'path': 'relative/dir'})
        assert bad.status_code == 409
        good = client.get('/api/backup/library/sizes', params={'path': root})
        assert good.status_code == 200
        rows = good.json()['sizes']
        assert rows[0]['path'] == root
        assert rows[0]['size_bytes'] > 0


def test_library_list_and_result_card_show_archive_size():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate("Ptu.bindEvents()")
        page.evaluate('''()=>{
          Ptu.State.loggedIn=true; Ptu.State.loginUid='10001';
          Ptu.Api.libraryList=async()=>({entries:[{path:'/book/a',uid:'20001',screen_name:'合成',available:true,total_posts:12,last_successful_sync_at:'2026-09-01T00:00:00'}]});
          Ptu.Api.librarySizes=async()=>({sizes:[{path:'/book/a',size_bytes:53687091200}]});
        }''')
        page.evaluate("Ptu.switchWorkspaceTab('library')")
        page.wait_for_function(
            "() => document.querySelector('#history-list .library-size')"
            " && document.querySelector('#history-list .library-size').textContent.includes('50.0 GB')"
        )
        page.evaluate('''()=>{
          Ptu.Api.librarySizes=async()=>({sizes:[{path:'/book/a',size_bytes:2147483648}]});
          Ptu.renderResult({task_kind:'personal_archive',output_dir:'/book/a',total_posts:5,new_posts:5});
        }''')
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#result-list li'))"
            ".some(li => li.textContent.includes('档案占用') && li.textContent.includes('2.0 GB'))"
        )
        browser.close()
