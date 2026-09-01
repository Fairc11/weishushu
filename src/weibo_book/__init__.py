"""微博书 - 把微博主页变成一本可以保存的电子书"""

from __future__ import annotations

import sys

# Windows terminal fix: ensure UTF-8 output for emoji support
# (handles both None stdout from packaged exe and GBK terminals)
if sys.stdout is not None:
    try:
        enc = sys.stdout.encoding
        if enc and enc.lower() not in ("utf-8", "utf8") and sys.stdout.buffer is not None:
            import io
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
            )
            if sys.stderr is not None and sys.stderr.buffer is not None:
                sys.stderr = io.TextIOWrapper(
                    sys.stderr.buffer,
                    encoding="utf-8",
                    errors="replace",
                )
    except (AttributeError, ValueError):
        pass

__version__ = "2.0.1"

from .api import WeiboBook
from .models import Post, Comment, PostMedia, UserInfo, MediaType, CommentType, BookConfig

__all__ = [
    "WeiboBook",
    "Post",
    "Comment",
    "PostMedia",
    "UserInfo",
    "MediaType",
    "CommentType",
    "BookConfig",
]
