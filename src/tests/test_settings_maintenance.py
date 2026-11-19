"""设置面板扩展：自动检查更新红点、任务通知开关、缓存清理、档案存储总览。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.main import app
from backend.app.services import maintenance
from test_backup_flow_ui import load_page, stub_common
from test_focused_workflow import app_harness, run_node
from weibo_book.errors import WeiboError


def _fake_roots(tmp_path, monkeypatch):
    update = tmp_path / 'cache' / 'update'
    logs = tmp_path / 'logs'
    update.mkdir(parents=True)
    logs.mkdir(parents=True)
    monkeypatch.setattr(maintenance, '_cache_dir', lambda: tmp_path / 'cache')
    monkeypatch.setattr(maintenance, '_log_dir', lambda: logs)
    return update, logs


def test_cache_items_and_clear_update_staging(tmp_path, monkeypatch):
    update, _logs = _fake_roots(tmp_path, monkeypatch)
    (update / 'v2.1.0').mkdir()
    (update / 'v2.1.0' / 'Weishushu.dmg').write_bytes(b'x' * 300)
    items = maintenance.cache_items()
    by_key = {row['key']: row for row in items}
    assert by_key['update']['size_bytes'] == 300
    result = maintenance.clear_cache_item('update')
    assert result['freed_bytes'] == 300
    assert list(update.iterdir()) == []
    assert update.is_dir()


def test_clear_logs_keeps_five_newest_files(tmp_path, monkeypatch):
    _update, logs = _fake_roots(tmp_path, monkeypatch)
    for index in range(8):
        path = logs / f'run-{index}.log'
        path.write_bytes(bytes([index]) * (10 + index))
        os_mtime = 1000000000 + index
        import os
        os.utime(path, (os_mtime, os_mtime))
    result = maintenance.clear_cache_item('logs')
    remaining = sorted(p.name for p in logs.iterdir())
    assert remaining == ['run-3.log', 'run-4.log', 'run-5.log', 'run-6.log', 'run-7.log']
    assert result['freed_bytes'] == 10 + 11 + 12


def test_clear_cache_rejects_unknown_key(tmp_path, monkeypatch):
    _fake_roots(tmp_path, monkeypatch)
    with pytest.raises(WeiboError):
        maintenance.clear_cache_item('playwright')


def test_notification_applescript_escapes_quotes(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    calls = []
    result = maintenance.send_notification(
        '微"书"薯', '备份完成：C:\\book\\"邓" 共 5 条', runner=calls.append
    )
    assert result == {'delivered': True}
    argv = calls[0]
    assert argv[0] == 'osascript'
    script = argv[2]
    assert '\\"' in script
    assert '微\\"书\\"薯' in script
    assert 'C:\\\\book' in script


def test_notification_reports_unsupported_platform(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    result = maintenance.send_notification('t', 'm', runner=lambda argv: None)
    assert result['delivered'] is False


def test_maintenance_routes(tmp_path, monkeypatch):
    _fake_roots(tmp_path, monkeypatch)
    monkeypatch.setattr(maintenance, 'send_notification', lambda title, message: {'delivered': True})
    with TestClient(app) as client:
        listing = client.get('/api/maintenance/cache')
        assert listing.status_code == 200
        assert {row['key'] for row in listing.json()['items']} == {'update', 'logs'}
        bad = client.post('/api/maintenance/cache/clear', json={'key': 'playwright'})
        assert bad.status_code == 409
        sent = client.post('/api/maintenance/notify', json={'title': '微书薯', 'message': '完成'})
        assert sent.status_code == 200
        assert sent.json()['delivered'] is True


def test_settings_flags_defaults_and_persistence():
    run_node(app_harness('''
const store = {};
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
if (Ptu.settingsFlag('weishushu.autoCheckUpdate', true) !== true) throw Error('自动检查更新应默认开启');
if (Ptu.settingsFlag('weishushu.notifyOnTaskDone', false) !== false) throw Error('任务通知应默认关闭');
Ptu.setSettingsFlag('weishushu.autoCheckUpdate', false);
if (store['weishushu.autoCheckUpdate'] !== '0') throw Error('开关未持久化');
if (Ptu.settingsFlag('weishushu.autoCheckUpdate', true) !== false) throw Error('关闭后读取错误');
'''))


def test_auto_check_marks_dot_only_when_update_exists():
    run_node(app_harness('''
const store = {};
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
element('settings-update-dot').hidden = true;
let called = 0;
Ptu.Api.updateCheck = async () => { called++; return {current:'2.1.0', latest:'v9.9.9', has_update:true}; };
(async () => {
  await Ptu.maybeAutoCheckUpdate();
  if (called !== 1) throw Error('未自动检查更新');
  if (element('settings-update-dot').hidden) throw Error('发现新版本未点亮红点');
  Ptu.State.updateInfo = null;
  Ptu.setSettingsFlag('weishushu.autoCheckUpdate', false);
  await Ptu.maybeAutoCheckUpdate();
  if (called !== 1) throw Error('关闭后仍自动检查更新');
  if (!element('settings-update-dot').hidden) throw Error('无更新信息时红点应隐藏');
})();
'''))


def test_task_done_notification_requires_switch():
    run_node(app_harness('''
const store = {};
global.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
const sent = [];
Ptu.Api.notify = async (title, message) => { sent.push(title + '|' + message); return {delivered:true}; };
(async () => {
  await Ptu.maybeNotifyTaskDone('微博书生成完成');
  if (sent.length !== 0) throw Error('默认不应发送通知');
  Ptu.setSettingsFlag('weishushu.notifyOnTaskDone', true);
  await Ptu.maybeNotifyTaskDone('微博书生成完成');
  if (sent.join(',') !== '微书薯|微博书生成完成') throw Error('通知内容错误：' + sent.join(','));
  Ptu.Api.notify = async () => { throw new Error('网络错误'); };
  await Ptu.maybeNotifyTaskDone('再次完成');
})();
'''))


def test_settings_markup_contains_new_sections():
    html = (Path(__file__).resolve().parents[1] / 'backend/app/templates/base.html').read_text(encoding='utf-8')
    for token in (
        'id="settings-auto-update-check"',
        'id="settings-notify-task"',
        'id="settings-cache-list"',
        'id="settings-storage-list"',
        'id="settings-update-dot"',
    ):
        assert token in html


def test_settings_panel_renders_cache_storage_and_switch_persistence():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        load_page(page)
        stub_common(page)
        page.evaluate("Ptu.bindEvents()")
        page.evaluate('''()=>{
          window.flagStore={};
          Ptu.safeLocalGet=k=>(k in window.flagStore?window.flagStore[k]:null);
          Ptu.safeLocalSet=(k,v)=>{window.flagStore[k]=v;return true};
          window.cleared=[];
          Ptu.Api.maintenanceCache=async()=>({items:[
            {key:'update',label:'更新包暂存',path:'/c/update',size_bytes:1048576},
            {key:'logs',label:'运行日志',path:'/c/logs',size_bytes:0},
          ]});
          Ptu.Api.maintenanceCacheClear=async key=>{window.cleared.push(key);return {key,freed_bytes:1048576}};
          Ptu.Api.libraryList=async()=>({entries:[{path:'/book/a',uid:'20001',screen_name:'合成',available:true,total_posts:12,last_successful_sync_at:'2026-09-01T00:00:00'}]});
          Ptu.Api.librarySizes=async()=>({sizes:[{path:'/book/a',size_bytes:53687091200}]});
        }''')
        page.evaluate("Ptu.toggleSettings()")
        page.wait_for_function("document.querySelectorAll('#settings-cache-list .settings-row').length === 2")
        page.wait_for_function("document.querySelectorAll('#settings-storage-list .settings-row').length === 1")
        cache_text = page.locator('#settings-cache-list').inner_text()
        assert '更新包暂存' in cache_text and '1.0 MB' in cache_text
        storage_text = page.locator('#settings-storage-list').inner_text()
        assert '@合成' in storage_text and '50.0 GB' in storage_text
        page.locator('#settings-cache-list .settings-row').first.get_by_role('button', name='清理').click()
        page.wait_for_function("window.cleared.join(',') === 'update'")
        assert any('已释放' in message for message in page.evaluate('window.toasts'))
        page.locator('#settings-storage-list .settings-row').first.get_by_role('button', name='定位').click()
        page.wait_for_function("window.toasts.some(m => m.includes('/book/a'))")
        page.locator('#settings-auto-update-check').click()
        assert page.evaluate("window.flagStore['weishushu.autoCheckUpdate']") == '0'
        page.locator('#settings-notify-task').click()
        assert page.evaluate("window.flagStore['weishushu.notifyOnTaskDone']") == '1'
        browser.close()
