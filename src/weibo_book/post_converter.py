"""微博帖子与媒体数据转换。"""

from __future__ import annotations

import logging

from crawl4weibo.models.post import Post as CrawlPost

from .external_fields import ExternalFieldAdapter
from .models import Comment, ImageQuality, LinkCard, MediaType, Post, PostMedia
from .raw_status import RAW_STATUS_KEY
from .text_content import weibo_html_to_text

logger = logging.getLogger(__name__)

_POST_CARD_FIELDS = ExternalFieldAdapter(
    {
        "verified_paths": {
            "is_pinned": ["mblog", "title", "text"],
            "ip_location": ["mblog", "region_name"],
        }
    }
)


def transform_image_url(url: str, quality: ImageQuality) -> str:
    """转换图片 URL 到指定清晰度。"""
    if not url:
        return url
    for size_prefix in ('mw2000/', 'mw1024/', 'mw690/', 'wap180/', 'thumb180/'):
        size_pattern = f'/{size_prefix}'
        if size_pattern in url:
            url = url.replace(size_pattern, '/')
            break
    target = quality.value
    for old_q in ('original', 'large', 'mw1024', 'mw690', 'wap180', 'thumb180'):
        old_pattern = f'/{old_q}/'
        new_pattern = f'/{target}/'
        if old_pattern in url:
            return url.replace(old_pattern, new_pattern)
    parts = url.rsplit('/', 1)
    if len(parts) == 2:
        return f'{parts[0]}/{target}/{parts[1]}'
    return url


def extract_media(crawl_post: CrawlPost, image_quality: ImageQuality = ImageQuality.ORIGINAL) -> list[PostMedia]:
    """从 crawl4weibo 帖子提取图片、实况照片和视频。"""
    media_list: list[PostMedia] = []
    seen_urls: set[str] = set()
    parsed_raw = crawl_post.raw_data or {}
    raw = parsed_raw.get(RAW_STATUS_KEY) or parsed_raw

    mix_items = (raw.get('mix_media_info') or {}).get('items') or []
    if mix_items:
        for item in mix_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get('type')
            data = item.get('data') or {}
            if item_type == 'pic':
                _append_picture_media(media_list, seen_urls, data, image_quality)
            elif item_type == 'video':
                _append_video_media(media_list, seen_urls, data)
            else:
                logger.warning('忽略未知混合媒体类型: %s', item_type)
        return media_list

    pics = raw.get('pics') or []
    if pics:
        for info in pics:
            if isinstance(info, dict):
                _append_picture_media(media_list, seen_urls, info, image_quality)
        if not any(isinstance(info, dict) and info.get('type') == 'video' for info in pics):
            page_info = raw.get('page_info') or {}
            if page_info.get('type') == 'video':
                _append_video_media(media_list, seen_urls, page_info)
        return media_list

    pic_infos = raw.get('pic_infos') or {}
    pic_ids = raw.get('pic_ids') or []
    if pic_infos and pic_ids:
        for pic_id in pic_ids:
            info = pic_infos.get(pic_id)
            if not info or not isinstance(info, dict):
                continue
            pic_type = info.get('type') or ''
            has_video = 'video' in info
            if pic_type in ('live', 'livephoto'):
                video_info = info.get('video', {}) or {}
                img_url = _get_best_image(info, image_quality)
                video_url = _get_embedded_video_url(info, video_info)
                if not video_url:
                    video_url = info.get('video_url', '')
                duration = video_info.get('duration') if isinstance(video_info, dict) else None
                duration = duration or info.get('duration')
                if img_url and video_url and img_url not in seen_urls:
                    seen_urls.add(img_url)
                    media_list.append(PostMedia(type=MediaType.LIVE_PHOTO, url=video_url, thumbnail=img_url, width=info.get('width', 0) or 0, height=info.get('height', 0) or 0, duration=duration))
            elif has_video and pic_type in ('', 'gif', 'video'):
                video_info = info.get('video', {}) or {}
                img_url = _get_best_image(info, image_quality)
                video_url = video_info.get('url') or video_info.get('mp4_url') or video_info.get('h264_url') or ''
                duration = video_info.get('duration') or info.get('duration')
                if img_url and video_url and img_url not in seen_urls:
                    seen_urls.add(img_url)
                    media_list.append(PostMedia(type=MediaType.VIDEO, url=video_url, thumbnail=img_url, duration=duration))
            else:
                img_url = _get_best_image(info, image_quality)
                thumb_url = _get_best_image(info, ImageQuality.THUMB)
                if img_url and img_url not in seen_urls:
                    seen_urls.add(img_url)
                    media_list.append(PostMedia(type=MediaType.IMAGE, url=img_url, thumbnail=thumb_url or img_url, width=info.get('width', 0) or 0, height=info.get('height', 0) or 0))
    else:
        for pic_url in crawl_post.pic_urls or []:
            if pic_url and pic_url not in seen_urls:
                seen_urls.add(pic_url)
                target_url = transform_image_url(pic_url, image_quality)
                media_list.append(PostMedia(type=MediaType.IMAGE, url=target_url, thumbnail=pic_url))

    if crawl_post.video_url:
        video_url = crawl_post.video_url
        if video_url not in seen_urls:
            seen_urls.add(video_url)
            thumbnail = None
            duration = None
            try:
                page_info = raw.get('page_info', {}) or {}
                if page_info:
                    if 'page_pic' in page_info:
                        thumbnail = page_info['page_pic'].get('url')
                    if 'media_info' in page_info:
                        duration = page_info['media_info'].get('duration')
                        if not video_url or 'weibo.com' not in video_url:
                            video_url = page_info.get('media_info', {}).get('mp4_h265_url') or page_info.get('media_info', {}).get('mp4_720p_mp4') or page_info.get('media_info', {}).get('mp4_sd_url') or video_url
            except Exception as exc:
                logger.debug('解析 page_info 视频 URL 失败: %s', exc)
            media_list.append(PostMedia(type=MediaType.VIDEO, url=video_url, thumbnail=thumbnail, duration=duration))

    try:
        page_info = raw.get('page_info', {}) or {}
        if page_info and page_info.get('type') == 'video':
            media_info = page_info.get('media_info', {}) or {}
            stream_url = media_info.get('mp4_h265_url') or media_info.get('mp4_720p_mp4') or media_info.get('mp4_sd_url') or media_info.get('stream_url') or ''
            if stream_url and stream_url not in seen_urls:
                seen_urls.add(stream_url)
                thumbnail = None
                if 'page_pic' in page_info:
                    thumbnail = page_info['page_pic'].get('url')
                media_list.append(PostMedia(type=MediaType.VIDEO, url=stream_url, thumbnail=thumbnail, duration=media_info.get('duration')))
    except Exception as exc:
        logger.debug('解析 page_info 视频流失败: %s', exc)
    return media_list


def _append_picture_media(
    media_list: list[PostMedia],
    seen_urls: set[str],
    info: dict,
    image_quality: ImageQuality,
) -> None:
    """按接口中的单项顺序追加图片、实况照片或视频。"""
    item_type = info.get('type') or info.get('pic_type') or ''
    image_url = _get_best_image(info, image_quality)
    thumb_url = _get_best_image(info, ImageQuality.THUMB) or image_url
    width, height = _get_dimensions(info)

    if item_type in ('live', 'livephoto'):
        video_info = info.get('video') or {}
        video_url = _get_embedded_video_url(info, video_info)
        if image_url and video_url and image_url not in seen_urls:
            seen_urls.update((image_url, video_url))
            duration = video_info.get('duration') if isinstance(video_info, dict) else None
            media_list.append(PostMedia(
                type=MediaType.LIVE_PHOTO,
                url=video_url,
                thumbnail=image_url,
                width=width,
                height=height,
                duration=duration or info.get('duration'),
            ))
        return

    if item_type == 'video':
        video_url = _get_embedded_video_url(info, info.get('video') or {})
        if video_url and video_url not in seen_urls:
            seen_urls.add(video_url)
            media_list.append(PostMedia(
                type=MediaType.VIDEO,
                url=video_url,
                thumbnail=image_url or None,
                width=width,
                height=height,
                duration=info.get('duration'),
            ))
        return

    if image_url and image_url not in seen_urls:
        seen_urls.add(image_url)
        media_list.append(PostMedia(
            type=MediaType.IMAGE,
            url=image_url,
            thumbnail=thumb_url,
            width=width,
            height=height,
        ))


def _append_video_media(media_list: list[PostMedia], seen_urls: set[str], data: dict) -> None:
    media_info = data.get('media_info') or {}
    video_url = _get_best_video_url(media_info)
    if not video_url or video_url in seen_urls:
        return
    seen_urls.add(video_url)
    page_pic = data.get('page_pic') or {}
    thumbnail = page_pic.get('url') if isinstance(page_pic, dict) else None
    media_list.append(PostMedia(
        type=MediaType.VIDEO,
        url=video_url,
        thumbnail=thumbnail,
        duration=media_info.get('duration'),
    ))


def _get_best_video_url(media_info: dict) -> str:
    playback_list = media_info.get('playback_list') or []
    playable = []
    for media in playback_list:
        meta = media.get('meta') or {}
        play_info = media.get('play_info') or {}
        quality_index = meta.get('quality_index')
        url = play_info.get('url')
        if isinstance(quality_index, (int, float)) and url:
            playable.append((quality_index, url))
    if playable:
        return max(playable, key=lambda entry: entry[0])[1]
    return (
        media_info.get('replay_hd')
        or media_info.get('stream_url_hd')
        or media_info.get('stream_url')
        or ''
    )


def _get_embedded_video_url(info: dict, video_info) -> str:
    if isinstance(video_info, str):
        return video_info
    if not isinstance(video_info, dict):
        video_info = {}
    return (
        info.get('videoSrc')
        or video_info.get('url')
        or video_info.get('mp4_url')
        or video_info.get('h264_url')
        or info.get('video_url')
        or ''
    )


def _get_dimensions(info: dict) -> tuple[int, int]:
    width = info.get('width', 0) or 0
    height = info.get('height', 0) or 0
    image_info = info.get('largest') or info.get('large') or {}
    geo = image_info.get('geo') if isinstance(image_info, dict) else {}
    if isinstance(geo, dict):
        width = width or geo.get('width', 0) or 0
        height = height or geo.get('height', 0) or 0
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 0
    return width, height


def _get_best_image(info: dict, quality: ImageQuality) -> str:
    """从 pic_infos 条目中获取指定清晰度的图片 URL。"""
    quality_map = {ImageQuality.THUMB: 'thumbnail', ImageQuality.MEDIUM: 'mw690', ImageQuality.LARGE: 'mw1024', ImageQuality.ORIGINAL: 'largest', ImageQuality.HQ: 'original'}
    field = quality_map.get(quality, 'largest')
    img_info = info.get(field, {}) or {}
    url = img_info.get('url') if isinstance(img_info, dict) else None
    if url:
        return url
    for fallback in ['largest', 'large', 'original', 'mw1024', 'mw690', 'thumbnail']:
        fallback_info = info.get(fallback, {}) or {}
        url = fallback_info.get('url') if isinstance(fallback_info, dict) else None
        if url:
            return url
    return info.get('url', '')


def extract_link_card(crawl_post: CrawlPost) -> LinkCard | None:
    """从原始 `page_info` 保留微博页面已返回的引用卡片字段。"""
    parsed_raw = crawl_post.raw_data or {}
    raw = parsed_raw.get(RAW_STATUS_KEY) or parsed_raw
    page_info = raw.get('page_info') or {}
    if not isinstance(page_info, dict):
        return None
    title = page_info.get('page_title') or ''
    page_url = page_info.get('page_url') or ''
    original_url = page_info.get('url_ori') or ''
    page_pic = page_info.get('page_pic') or {}
    image_url = page_pic.get('url') if isinstance(page_pic, dict) else ''
    if not any((title, page_url, original_url, image_url)):
        return None
    return LinkCard(
        type=page_info.get('type') or '',
        title=title,
        description=page_info.get('content1') or '',
        image_url=image_url or '',
        url=page_url or original_url,
        original_url=original_url,
    )


def extract_post_text(crawl_post: CrawlPost) -> str:
    """优先从项目保存的精确原始状态恢复微博正文换行。"""
    parsed_raw = crawl_post.raw_data or {}
    raw = parsed_raw.get(RAW_STATUS_KEY)
    if isinstance(raw, dict):
        raw_text = raw.get('text')
        if isinstance(raw_text, str):
            return weibo_html_to_text(raw_text)
    return crawl_post.text or ''


def crawl_post_to_our_post(crawl_post: CrawlPost, target_uid: str, comments: list[Comment] | None = None, image_quality: ImageQuality = ImageQuality.ORIGINAL) -> Post:
    """将 crawl4weibo 的帖子转换为项目帖子模型。"""
    media = extract_media(crawl_post, image_quality)
    retweeted = None
    if crawl_post.retweeted_status:
        retweeted = crawl_post_to_our_post(crawl_post.retweeted_status, target_uid, image_quality=image_quality)
    parsed_raw = crawl_post.raw_data or {}
    raw = parsed_raw.get(RAW_STATUS_KEY) or parsed_raw
    raw_user = raw.get('user') or {}
    verified_raw = parsed_raw.get(RAW_STATUS_KEY)
    verified_card_payload = {
        "mblog": verified_raw if isinstance(verified_raw, dict) else {}
    }
    return Post(
        bid=crawl_post.bid,
        uid=str(crawl_post.user_id),
        user_name=raw_user.get('screen_name') or '',
        user_avatar=raw_user.get('profile_image_url') or raw_user.get('avatar_hd') or '',
        text=extract_post_text(crawl_post),
        created_at=crawl_post.created_at,
        source=crawl_post.source or '',
        media=media,
        reposts_count=crawl_post.reposts_count or 0,
        comments_count=crawl_post.comments_count or 0,
        likes_count=crawl_post.attitudes_count or 0,
        is_original=crawl_post.is_original,
        retweeted=retweeted,
        comments=comments or [],
        location=crawl_post.location or '',
        raw_bid=crawl_post.bid,
        verified=bool(raw_user.get('verified', False)),
        gender=raw_user.get('gender') or '',
        link_card=extract_link_card(crawl_post),
        ip_location=_POST_CARD_FIELDS.read(verified_card_payload, "ip_location") or "",
        is_pinned=(
            _POST_CARD_FIELDS.read(verified_card_payload, "is_pinned") == "置顶"
        ),
    )
