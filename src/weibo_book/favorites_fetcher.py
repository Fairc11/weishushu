"""微博收藏抓取。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from crawl4weibo.models.post import Post as CrawlPost

from .errors import classify_error, get_retry_delay, is_recoverable
from .models import ImageQuality, Post

logger = logging.getLogger(__name__)


PostConverter = Callable[..., Post]
SleepFunc = Callable[[float], None]


@dataclass
class FavoritesFetchResult:
    posts: list[Post] = field(default_factory=list)
    pages_total: int = 0
    pages_failed: int = 0
    partial: bool = False
    partial_reason: str = ""
    skipped_bids: list[dict[str, str]] = field(default_factory=list)


def fetch_favorites(
    client: Any,
    uid: str,
    screen_name: str,
    max_posts: int = 0,
    image_quality: ImageQuality = ImageQuality.ORIGINAL,
    post_converter: PostConverter | None = None,
    sleep_func: SleepFunc = time.sleep,
) -> FavoritesFetchResult:
    """获取用户收藏微博并返回抓取状态。"""
    if post_converter is None:
        raise ValueError("post_converter 不能为空")

    result = FavoritesFetchResult()
    page = 1
    total_fetched = 0
    logger.info(f'正在提取 @{screen_name} 的收藏微博...')
    max_attempts = 3

    while True:
        attempt = 0
        data = None
        while attempt < max_attempts:
            try:
                resp = client.session.get(
                    'https://m.weibo.cn/api/favorites/get_favorites',
                    params={'page': page},
                    timeout=15,
                )
                data = resp.json()
                break
            except Exception as e:
                attempt += 1
                kind = classify_error(e)
                if attempt >= max_attempts:
                    result.partial = True
                    result.pages_failed += 1
                    result.partial_reason = f'第{page}页收藏连续{max_attempts}次失败[{kind.value}]: {e}'
                    logger.warning(f'  ⚠️ {result.partial_reason}（已部分提取 {len(result.posts)} 条）')
                    return result
                if not is_recoverable(kind) and attempt > 0:
                    result.partial = True
                    result.pages_failed += 1
                    result.partial_reason = f'第{page}页收藏失败[{kind.value}]: {e}'
                    logger.warning(f'  ⚠️ {result.partial_reason}（已部分提取 {len(result.posts)} 条）')
                    return result
                delay = get_retry_delay(attempt, base=2.0, max_delay=60.0)
                logger.info(f'  第 {page} 页收藏失败({attempt}/{max_attempts}) [{kind.value}]: {e}, {delay:.1f}s 后重试')
                sleep_func(delay)

        if data is None:
            break
        if data.get('ok') != 1:
            logger.info(f'  第 {page} 页: 无更多收藏或需要登录')
            break
        favorites_list = data.get('data', {}).get('favorites', [])
        if not favorites_list:
            break

        result.pages_total += 1
        for fav in favorites_list:
            status = fav.get('status', {})
            if not status:
                continue
            crawl_dict = _status_to_crawl_dict(status, uid)
            try:
                crawl_post = CrawlPost.from_dict(crawl_dict)
            except Exception as exc:
                skip_bid = crawl_dict.get('bid') or crawl_dict.get('id') or 'unknown'
                result.skipped_bids.append({'bid': str(skip_bid), 'reason': f'CrawlPost.from_dict: {exc}'})
                logger.debug(f'  跳过收藏 {skip_bid}: {exc}')
                continue

            our_post = post_converter(crawl_post, uid, image_quality=image_quality)
            author_info = status.get('user', {}) or {}
            our_post.user_name = author_info.get('screen_name', '')
            our_post.user_avatar = author_info.get('avatar_hd', '')
            our_post.uid = str(author_info.get('id', ''))
            result.posts.append(our_post)
            total_fetched += 1
            if max_posts > 0 and total_fetched >= max_posts:
                break

        logger.info(f'  第 {page} 页: 已获取 {len(favorites_list)} 条收藏')
        if max_posts > 0 and total_fetched >= max_posts:
            break
        page += 1
        sleep_func(0.5)

    logger.info(f'\n📊 共提取 {len(result.posts)} 条收藏微博')
    return result


def _status_to_crawl_dict(status: dict[str, Any], uid: str) -> dict[str, Any]:
    crawl_dict = {
        'id': status.get('id', ''),
        'bid': status.get('bid', ''),
        'user_id': str(status.get('user', {}).get('id', uid)),
        'text': status.get('text', ''),
        'created_at': status.get('created_at', ''),
        'source': status.get('source', ''),
        'reposts_count': status.get('reposts_count', 0),
        'comments_count': status.get('comments_count', 0),
        'attitudes_count': status.get('attitudes_count', 0),
        'pic_ids': status.get('pic_ids', []),
        'pic_infos': status.get('pic_infos', {}),
        'pic_urls': [f'https://wx1.sinaimg.cn/large/{pid}.jpg' for pid in status.get('pic_ids') or []],
        'video_url': (
            status.get('page_info', {}).get('media_info', {}).get('stream_url', '')
            or status.get('page_info', {}).get('media_info', {}).get('mp4_sd_url', '')
            or ''
        ),
        'is_original': not bool(status.get('retweeted_status')),
        'retweeted_status': status.get('retweeted_status'),
        'page_info': status.get('page_info', {}),
        'location': '',
        'topic_ids': [],
        'at_users': [],
        'is_long_text': False,
    }
    retweeted = status.get('retweeted_status')
    if retweeted:
        retweeted['user_id'] = str(retweeted.get('user', {}).get('id', ''))
        crawl_dict['retweeted_status'] = retweeted
    return crawl_dict
