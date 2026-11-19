from weibo_book.archive.repository import ArchiveRepository
from weibo_book.archive.render_snapshot import ArchiveRenderer
from weibo_book.archive.schema import PostRecord


def _render(root):
    repository = ArchiveRepository.open(root, '10001')
    try:
        ArchiveRenderer(repository).render_all(
            root, render_pdf=lambda _html, pdf: pdf.write_bytes(b'PDF'))
    finally:
        repository.close()


def test_render_writes_readme_once_and_never_overwrites(tmp_path):
    root = tmp_path / 'book'
    repository = ArchiveRepository.create(root, '10001', '本人')
    repository.upsert_post(PostRecord('B1', '10001', '正文', created_at='2026-09-01T00:00:00+00:00'))
    repository.close()

    _render(root)
    readme = root / '!请先阅读.txt'
    assert readme.is_file()
    text = readme.read_text(encoding='utf-8-sig')
    for key in ['微书薯', '微博书.html', '数据与媒体', 'zip', '新增备份',
                'https://github.com/Fairc11/weishushu']:
        assert key in text

    readme.write_text('用户自己改过的内容', encoding='utf-8')
    _render(root)
    assert readme.read_text(encoding='utf-8') == '用户自己改过的内容'
