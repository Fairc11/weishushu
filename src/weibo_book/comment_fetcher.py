"""微博评论抓取与转换。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from crawl4weibo.models.comment import Comment as CrawlComment

from .errors import WeiboError, classify_error
from .models import Comment

logger = logging.getLogger(__name__)


def convert_crawl_comment(c: CrawlComment, blogger_uid: str) -> Comment:
    """将 crawl4weibo 的 Comment 转换为我们的 Comment 模型"""
    return Comment(
        id=c.id,
        text=c.text,
        user_name=c.user_screen_name,
        user_id=c.user_id,
        user_avatar=c.user_avatar_url,
        created_at=str(c.created_at) if c.created_at else '',
        like_counts=c.like_counts,
        is_blogger=c.user_id == blogger_uid,
        source=c.source,
        image_url=c.pic_url,
        parent_id=c.reply_id,
    )


def _parsed_comment_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for pattern in (
        "%a %b %d %H:%M:%S %z %Y",
        "%Y-%m-%d %H:%M:%S %z",
    ):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _recent_parents(comments: list[Comment], count: int) -> list[Comment]:
    parsed: list[tuple[datetime, Comment]] = []
    for comment in comments:
        created_at = _parsed_comment_time(comment.created_at)
        if created_at is None:
            logger.debug(
                "无法解析评论时间，整页保持 API 一级评论相对顺序: id=%s",
                comment.id,
            )
            return comments[: min(count, 10)]
        parsed.append((created_at, comment))

    awareness = {item[0].utcoffset() is not None for item in parsed}
    if len(awareness) > 1:
        logger.debug("评论时间时区状态混合，整页保持 API 一级评论相对顺序")
        return comments[: min(count, 10)]

    if awareness == {True}:
        parsed.sort(key=lambda item: item[0].astimezone(timezone.utc), reverse=True)
    else:
        parsed.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in parsed[: min(count, 10)]]


def fetch_post_comments(
    client: Any,
    post_id: str,
    blogger_uid: str,
    count: int = 5,
    comments_type: str = 'hot',
) -> list[Comment]:
    """获取帖子的评论。"""
    if count <= 0:
        return []
    try:
        if comments_type == 'all':
            all_comments = client.get_all_comments(post_id, max_pages=1)
        else:
            comment_list, _ = client.get_comments(post_id, page=1)
            all_comments = comment_list
    except Exception as e:
        logger.info(f'    评论提取失败: {e}')
        return []
    return _convert_comment_response(all_comments, blogger_uid, count, comments_type)


def fetch_post_comments_strict(
    client: Any,
    post_id: str,
    blogger_uid: str,
    count: int = 5,
    comments_type: str = 'hot',
) -> list[Comment]:
    if count <= 0:
        return []
    try:
        if comments_type == 'all':
            all_comments = client.get_all_comments(post_id, max_pages=1)
        else:
            all_comments, _ = client.get_comments(post_id, page=1)
    except Exception as exc:
        raise WeiboError("读取微博评论失败", kind=classify_error(exc), original=exc) from exc
    return _convert_comment_response(all_comments, blogger_uid, count, comments_type)


def _convert_comment_response(
    all_comments: list[CrawlComment],
    blogger_uid: str,
    count: int,
    comments_type: str,
) -> list[Comment]:
    converted: list[Comment] = []
    seen_ids: set[str] = set()
    for c in all_comments:
        our_comment = convert_crawl_comment(c, blogger_uid)
        if our_comment.id in seen_ids:
            logger.warning("忽略同页重复评论 ID: %s", our_comment.id)
            continue
        seen_ids.add(our_comment.id)
        converted.append(our_comment)

    parents = [comment for comment in converted if not comment.parent_id]
    parent_by_id = {comment.id: comment for comment in parents}
    for reply in converted:
        if not reply.parent_id:
            continue
        if reply.parent_id == reply.id:
            logger.warning("忽略自指评论回复: %s", reply.id)
            continue
        parent = parent_by_id.get(reply.parent_id)
        if parent is not None:
            parent.replies.append(reply)

    if comments_type == 'blogger':
        filtered_parents: list[Comment] = []
        for parent in parents:
            blogger_replies = [reply for reply in parent.replies if reply.is_blogger]
            if parent.is_blogger or blogger_replies:
                parent.replies = blogger_replies
                filtered_parents.append(parent)
        parents = filtered_parents

    return _recent_parents(parents, count)
