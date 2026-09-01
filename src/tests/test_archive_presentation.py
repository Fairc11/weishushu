from weibo_book.archive.presentation import (
    format_archive_time,
    normalize_archive_text,
)


def test_format_archive_time_removes_iso_timezone_syntax():
    assert format_archive_time("2026-07-14T16:38:01+08:00") == "2026-07-14 16:38"


def test_format_archive_time_preserves_relative_comment_time():
    assert format_archive_time("5分钟前") == "5分钟前"


def test_normalize_archive_text_decodes_entities_and_labels_voice_duration():
    assert (
        normalize_archive_text("抽到新角色 [语音评论3&quot;]")
        == "抽到新角色 语音评论（3秒）"
    )
    assert normalize_archive_text("&quot;好好&quot;") == '"好好"'
