import pytest
from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.schema import PostRecord


def test_timings_include_running_scopes_without_double_counting():
    from weibo_book.archive.performance import PerformanceTimings
    now = [0.0]
    clock = PerformanceTimings(monotonic=lambda: now[0])
    with clock.measure('detail'):
        now[0] = 2
        assert clock.snapshot()['detail'] == 2
        now[0] = 5
    assert clock.snapshot()['detail'] == 5
    with pytest.raises(ValueError):
        with clock.measure('detail'):
            now[0] = 6
            raise ValueError()
    assert clock.snapshot()['detail'] == 6


def test_html_generation_reports_actual_steps_counts_and_publication(tmp_path):
    from weibo_book.archive.render_snapshot import ArchiveRenderer
    repo = ArchiveRepository.create(tmp_path/'archive', '10001', '测试')
    repo.upsert_post(PostRecord(bid='A', uid='10001', text='正文'))
    events = []
    outputs = ArchiveRenderer(repo).render_all(repo.root_path(), include_pdf=False,
                    include_markdown=False, progress_callback=events.append)
    subphases = [e['subphase'] for e in events]
    for subphase in ('snapshot', 'html', 'data', 'publish', 'cleanup'):
        assert subphase in subphases
    assert 'pdf' not in subphases
    published = [e for e in events if e['subphase'] == 'publish']
    assert published[-1]['current'] == published[-1]['total'] == len(outputs)
    assert all(e['current'] <= e['total'] for e in events if e['total'] is not None)
    assert all(e['phase'] == 'generate' for e in events)
    repo.close()


def test_scheduler_separates_request_and_wait_timings():
    from weibo_book.archive.performance import PerformanceTimings
    from weibo_book.archive.pacing import AdaptiveRequestScheduler
    now = [0.0]
    timings = PerformanceTimings(monotonic=lambda: now[0])
    def advance(seconds): now[0] += seconds
    scheduler = AdaptiveRequestScheduler('standard', monotonic=lambda:now[0], wait=advance, timings=timings)
    scheduler.run('detail', lambda: advance(3))
    scheduler.wait_for_retry('detail', 2)
    assert timings.snapshot() == {'request':3, 'wait':2}


@pytest.mark.parametrize('module', ['backend.app.services.personal_archive_tasks', 'backend.app.routers.router_backup'])
def test_production_render_entry_forwards_step_progress(tmp_path, module):
    import importlib
    renderer = importlib.import_module(module).render_personal_archive
    repo = ArchiveRepository.create(tmp_path/'archive', '10001', '测试')
    root=repo.root_path();repo.close()
    events=[]
    renderer(str(root), '10001', lambda:False, progress_callback=events.append)
    assert {'html','data','publish','cleanup'} <= {event['subphase'] for event in events}
