"""浏览器兜底抓取。"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .models import Post, UserInfo

logger = logging.getLogger(__name__)

SleepFunc = Callable[[float], None]


def get_browser_user_agent(platform: str | None = None) -> str:
    """返回与目标平台匹配的浏览器兜底 UA。"""
    target = platform or sys.platform
    if target == "darwin":
        platform_part = "Macintosh; Intel Mac OS X 10_15_7"
    elif target.startswith("win"):
        platform_part = "Windows NT 10.0; Win64; x64"
    else:
        platform_part = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_part}) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


def browser_post_to_our_post(data: dict, user: UserInfo) -> Optional[Post]:
    """将浏览器兜底抓到的页面摘要转换为基础 Post。"""
    bid = str(data.get('mid') or '').strip()
    text = str(data.get('text') or '').strip()
    if not bid and not text:
        return None
    likes_text = str(data.get('likes') or '0')
    likes_match = re.search('\\d+', likes_text.replace(',', ''))
    likes_count = int(likes_match.group(0)) if likes_match else 0
    return Post(
        bid=bid or f'browser_{abs(hash(text))}',
        uid=user.uid,
        user_name=user.screen_name,
        user_avatar=user.avatar_url,
        text=text,
        created_at=_parse_relative_time(str(data.get('time') or '')),
        source='浏览器兜底',
        likes_count=likes_count,
        is_original=True,
        raw_bid=bid,
    )


def fetch_posts_browser(
    client,
    user: UserInfo,
    uid: str,
    max_posts: int = 0,
    start_dt=None,
    end_dt=None,
    only_original: bool = False,
    has_cookies: bool = False,
    sleep_func: SleepFunc = time.sleep,
    sync_playwright_factory=None,
) -> list[Post]:
    """通过 Playwright 浏览器直接提取帖子。"""
    our_posts: list[Post] = []
    if sync_playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.info('  ⚠️ 需要安装 playwright: pip install playwright && playwright install chromium')
            return our_posts
        sync_playwright_factory = sync_playwright

    logger.info(f'  打开浏览器进入 @{user.screen_name} 的主页...')
    with sync_playwright_factory() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=get_browser_user_agent(),
            viewport={'width': 1280, 'height': 2000},
            locale='zh-CN',
        )
        if has_cookies and hasattr(client, 'session'):
            for c in client.session.cookies:
                if c.name in ('SUB', 'SUBP', 'SCF', 'XSRF-TOKEN', 'WBPSESS', 'PC_TOKEN'):
                    try:
                        context.add_cookies([{'name': c.name, 'value': c.value, 'domain': '.weibo.com', 'path': '/'}])
                    except Exception as exc:
                        logger.debug('注入 cookie %s 失败: %s', c.name, exc)
        page = context.new_page()
        url = f'https://weibo.com/u/{uid}'
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        sleep_func(3)
        fetched = 0
        seen_bids: set[str] = set()
        prev_count = -1
        scroll_attempts = 0
        max_scroll = 30
        while (max_posts == 0 or fetched < max_posts) and scroll_attempts < max_scroll:
            posts_data = page.evaluate('() => {\n                    const cards = document.querySelectorAll(\'[mid]\');\n                    return Array.from(cards).slice(0, 50).map(card => {\n                        const textEl = card.querySelector(\'.WB_text, [node-type="feed_list_content"]\');\n                        const text = textEl ? textEl.textContent.trim() : \'\';\n                        const mid = card.getAttribute(\'mid\') || \'\';\n                        const timeEl = card.querySelector(\'.WB_time, [node-type="feed_list_item_date"]\');\n                        const time = timeEl ? timeEl.textContent.trim() : \'\';\n                        const likeEl = card.querySelector(\'[node-type="like_status"]\');\n                        const likes = likeEl ? (likeEl.textContent || \'\').trim() : \'0\';\n                        return { mid, text: text.slice(0, 200), time, likes };\n                    });\n                }')
            if not posts_data:
                break
            for item in posts_data:
                post = browser_post_to_our_post(item, user)
                if not post:
                    continue
                key = post.raw_bid or post.bid
                if key in seen_bids:
                    continue
                seen_bids.add(key)
                pd = post.created_at
                if start_dt and pd and pd < start_dt:
                    continue
                if end_dt and pd and pd >= end_dt:
                    continue
                if only_original and not post.is_original:
                    continue
                our_posts.append(post)
                fetched = len(our_posts)
                if max_posts > 0 and fetched >= max_posts:
                    break
            current_count = len(posts_data)
            if current_count == prev_count:
                scroll_attempts += 1
            else:
                scroll_attempts = 0
            prev_count = current_count
            if max_posts > 0 and fetched >= max_posts:
                break
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            sleep_func(1.5)
        logger.info(f'  浏览器获取到 {len(our_posts)} 条帖子')
        browser.close()
    return our_posts


def _parse_relative_time(text):
    now = datetime.now(timezone(timedelta(hours=8)))
    text = text.strip()
    m = re.search('(\\d+)分钟前', text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search('(\\d+)小时前', text)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    if '昨天' in text:
        m = re.search('(\\d+):(\\d+)', text)
        if m:
            y = now - timedelta(days=1)
            return y.replace(hour=int(m.group(1)), minute=int(m.group(2)))
    for fmt in ('%m月%d日 %H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt.startswith('%m'):
                dt = dt.replace(year=now.year)
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            if dt > now:
                dt = dt.replace(year=dt.year - 1)
            return dt
        except ValueError:
            continue
    return None
