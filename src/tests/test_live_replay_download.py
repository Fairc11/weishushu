from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from weibo_book.errors import OperationPaused, WeiboError

TEXT = '#EXTM3U\n' + ''.join(f'#EXTINF:1,\ns{i}.ts\n' for i in range(8)) + '#EXT-X-ENDLIST\n'
NAMES = [f'B_live_0123456789abcdef_seg{i:04d}.ts' for i in range(8)]


def run_download(root, download, **kwargs):
    from weibo_book.archive.replay_download import download_replay_segments
    return download_replay_segments(
        root, 'https://media.invalid/live/list.m3u8', TEXT, NAMES,
        client=None, download=download, check_control=lambda: None, **kwargs,
    )


@pytest.mark.parametrize('workers', [2, 4])
def test_parallel_segments_are_bounded_and_return_in_playlist_order(tmp_path, workers):
    lock = threading.Lock()
    active = peak = 0
    barrier = threading.Barrier(workers)
    events = []
    def download(client, url, dest, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        dest.write_bytes(url.encode())
        with lock:
            active -= 1
        return True
    result = run_download(tmp_path/'cache', download, max_workers=workers, progress=events.append)
    assert peak == workers
    assert list(result.names) == NAMES
    assert [p.read_bytes() for p in result.files] == [u.encode() for u in result.urls]
    assert events[-1]['current'] == 8
    assert events[-1]['total'] == 8
    assert events[-1]['completed_bytes'] == sum(p.stat().st_size for p in result.files)


def test_failed_run_reuses_only_verified_segments_and_redownloads_corruption(tmp_path):
    calls = []
    def fail(client, url, dest, **kwargs):
        calls.append(url)
        if url.endswith('s3.ts'):
            return False
        dest.write_bytes(url.encode())
        return True
    with pytest.raises(WeiboError):
        run_download(tmp_path/'cache', fail, max_workers=1)
    completed = sorted((tmp_path/'cache').rglob('*.ts'))
    assert len(completed) == 3
    completed[0].write_bytes(b'corrupted')
    calls.clear()
    def success(client, url, dest, **kwargs):
        calls.append(url)
        dest.write_bytes(url.encode())
        return True
    result = run_download(tmp_path/'cache', success, max_workers=1)
    assert len(calls) == 6
    assert not any(u.endswith(('s1.ts', 's2.ts')) for u in calls)
    assert len(result.files) == 8


def test_pause_keeps_completed_files_for_resume(tmp_path):
    def download(client, url, dest, **kwargs):
        if url.endswith('s2.ts'):
            raise OperationPaused('任务已暂停')
        dest.write_bytes(b'segment')
        return True
    with pytest.raises(OperationPaused):
        run_download(tmp_path/'cache', download, max_workers=1)
    assert len(list((tmp_path/'cache').rglob('*.ts'))) == 2


def test_cache_symlink_is_rejected_without_touching_target(tmp_path):
    from tests.symlink_capability import require_symlink_capability
    require_symlink_capability()
    target = tmp_path/'outside'; target.mkdir()
    cache = tmp_path/'cache'; cache.symlink_to(target, target_is_directory=True)
    with pytest.raises(WeiboError):
        run_download(cache, lambda *a, **k: pytest.fail('must not download'))
    assert list(target.iterdir()) == []


def test_low_intensity_serializes_and_uses_scheduler(tmp_path):
    class Scheduler:
        is_low_intensity = True
        def __init__(self): self.count=0; self.requests=0
        def add_media_requests(self, count): self.count+=count
        def run(self, kind, operation):
            assert kind == 'media'
            self.requests+=1
            return operation()
    scheduler = Scheduler()
    threads = set()
    def download(client, url, dest, **kwargs):
        threads.add(threading.get_ident())
        dest.write_bytes(b'segment')
        return True
    run_download(tmp_path/'cache', download, scheduler=scheduler)
    assert len(threads) == 1
    assert scheduler.requests == scheduler.count == 8


def test_stream_pause_propagates_without_network_retry(tmp_path):
    import httpx
    from weibo_book.media import download_file
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * 65536
            yield b'y' * 65536
    class Signal:
        calls=0
        def is_set(self):
            self.calls += 1
            if self.calls >= 3:
                raise OperationPaused('任务已暂停')
            return False
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Body()))) as client:
        with pytest.raises(OperationPaused):
            download_file(client,'https://media.invalid/a.ts',tmp_path/'segment.ts',
                          cancel_event=Signal(),max_retries=0,raise_request_errors=True)
    assert not list(tmp_path.iterdir())
