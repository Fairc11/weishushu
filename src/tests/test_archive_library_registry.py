"""本地登记只保存身份和位置；实际数据每次只读核验。"""
import json
import pytest
from backend.app.services import archive_folder
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.errors import WeiboError


def archive(path, uid='10001'):
    repo = ArchiveRepository.create(path, uid=uid, screen_name='合成用户')
    repo.close()
    return str(path)


def test_registry_keeps_copies_and_reloads_metadata(tmp_path, monkeypatch):
    from backend.app.services import archive_library as library
    monkeypatch.setattr(library, '_registry_path', lambda: tmp_path / 'state' / 'archive-library.json')
    first = archive(tmp_path / 'first')
    second = archive(tmp_path / 'second')
    library.register_archive(first, expected_uid='10001')
    library.register_archive(second, expected_uid='10001')
    library.register_archive(first, expected_uid='10001')
    entries = library.list_archives()
    assert {e.path for e in entries} == {first, second}
    assert all(e.available for e in entries)
    stored = json.loads(library._registry_path().read_text())
    assert all(set(row) == {'path', 'uid'} for row in stored['entries'])
    (tmp_path / 'first').rename(tmp_path / 'moved')
    assert not next(e for e in library.list_archives() if e.path == first).available
    wrong = archive(tmp_path / 'wrong', '10002')
    with pytest.raises(WeiboError):
        library.relocate_archive(first, '10001', wrong)
    library.relocate_archive(first, '10001', str(tmp_path / 'moved'))
    library.remove_archive(str(tmp_path / 'moved'), '10001')
    assert (tmp_path / 'moved').exists()
    assert len(library.list_archives()) == 1


def test_corrupt_registry_is_not_overwritten(tmp_path, monkeypatch):
    from backend.app.services import archive_library as library
    registry = tmp_path / 'archive-library.json'
    registry.write_text('not json')
    monkeypatch.setattr(library, '_registry_path', lambda: registry)
    with pytest.raises(WeiboError):
        library.register_archive(archive(tmp_path / 'book'))
    assert registry.read_text() == 'not json'


def test_list_archives_migrates_legacy_self_folder_name(tmp_path, monkeypatch):
    from backend.app.services import archive_library as library
    monkeypatch.setattr(library, '_registry_path', lambda: tmp_path / 'state' / 'archive-library.json')
    legacy = tmp_path / '我的微博'
    archive(legacy)
    library.register_archive(str(legacy), expected_uid='10001')
    expected = str(tmp_path / '合成用户_10001')
    entries = library.list_archives()
    assert entries[0].path == expected
    assert entries[0].available
    stored = json.loads(library._registry_path().read_text())
    assert stored['entries'][0]['path'] == expected
    assert (tmp_path / '合成用户_10001').is_dir()
