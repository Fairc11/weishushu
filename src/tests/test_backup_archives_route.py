"""微博书库列表端点：只读列出目录下的档案，不限 UID。"""
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from tests.test_archive_folder import _create_archive


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_archives_lists_child_archives_and_direct_archive(client, tmp_path):
    _create_archive(tmp_path / "甲_10001", uid="10001")
    _create_archive(tmp_path / "乙_20002", uid="20002")
    (tmp_path / "普通文件夹").mkdir()
    (tmp_path / "普通文件夹" / "a.txt").write_text("x")

    r = client.post("/api/backup/archives", json={"output_dir": str(tmp_path)})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert {e["uid"] for e in entries} == {"10001", "20002"}
    assert all(e["screen_name"] for e in entries)
    assert all(e["total_posts"] == 1 for e in entries)

    r = client.post("/api/backup/archives", json={"output_dir": str(tmp_path / "甲_10001")})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["uid"] == "10001"


def test_archives_empty_and_plain_dirs(client, tmp_path):
    r = client.post("/api/backup/archives", json={"output_dir": str(tmp_path)})
    assert r.status_code == 200
    assert r.json()["entries"] == []
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x")
    r = client.post("/api/backup/archives", json={"output_dir": str(plain)})
    assert r.status_code == 200
    assert r.json()["entries"] == []
