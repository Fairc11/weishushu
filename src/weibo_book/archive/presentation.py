"""微博归档的稳定展示值。"""

from __future__ import annotations

import html
import re
from datetime import datetime


_VOICE_COMMENT = re.compile(r'\[语音评论\s*(\d+)\s*"\]')


def format_archive_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M")


def normalize_archive_text(value: str) -> str:
    text = html.unescape(str(value or ""))
    return _VOICE_COMMENT.sub(
        lambda match: f"语音评论（{match.group(1)}秒）",
        text,
    )
