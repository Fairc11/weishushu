"""Embedded browser boundary for the desktop shell."""

from .policy import is_allowed_browser_url, is_allowed_weibo_host
from .state import BrowserLayoutState, clamp_split_position

__all__ = (
    "BrowserLayoutState",
    "clamp_split_position",
    "is_allowed_browser_url",
    "is_allowed_weibo_host",
)
