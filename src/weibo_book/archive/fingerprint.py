"""微博内容指纹。"""

from __future__ import annotations

import hashlib
import json

from .schema import PostRecord


CONTENT_FIELDS = (
    "text",
    "source",
    "ip_location",
    "is_pinned",
    "visibility",
    "retweeted_payload",
    "link_card_payload",
    "media_signature",
)

_RETWEETED_NON_CONTENT_FIELDS = frozenset({
    "user_avatar",
    "reposts_count",
    "comments_count",
    "likes_count",
})


def _retweeted_content(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    return {
        key: value
        for key, value in payload.items()
        if key not in _RETWEETED_NON_CONTENT_FIELDS
    }


def content_fingerprint(post: PostRecord) -> str:
    payload = {field_name: getattr(post, field_name) for field_name in CONTENT_FIELDS}
    payload["retweeted_payload"] = _retweeted_content(post.retweeted_payload)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
