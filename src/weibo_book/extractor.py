"""微博书 - 数据提取模块

使用 crawl4weibo 从微博提取用户信息和帖子数据，
转换为项目自定义的数据模型。
"""
from __future__ import annotations
import logging
import threading
import time
from importlib.metadata import version as package_version
from functools import wraps
from datetime import datetime, timezone, timedelta
from typing import Optional
logger = logging.getLogger(__name__)
import crawl4weibo
import crawl4weibo.models
import crawl4weibo.models.post
from crawl4weibo import WeiboClient
from crawl4weibo import RateLimitConfig
from .browser_fallback import browser_post_to_our_post, fetch_posts_browser
from .comment_fetcher import convert_crawl_comment, fetch_post_comments
from .favorites_fetcher import fetch_favorites
from .login import load_cookies, cookies_to_header
from .errors import OperationCancelled, WeiboError, WeiboErrorKind, classify_error, is_recoverable, get_retry_delay, network_check, retry_with_backoff
from .post_converter import crawl_post_to_our_post, extract_media, transform_image_url
from .raw_status import RAW_STATUS_KEY
from .url_parser import parse_uid_from_url

from .models import Comment, ExtractType, ImageQuality, Post, UserInfo
_tls_cached_client = threading.local()


def preserve_raw_status(parser) -> None:
    """让依赖解析结果携带其对应的原始微博状态。"""
    original = parser._parse_single_post
    if getattr(original, "_weishushu_preserves_raw_status", False):
        return

    @wraps(original)
    def parse_with_raw_status(status):
        parsed = original(status)
        if parsed is not None:
            parsed[RAW_STATUS_KEY] = status
        return parsed

    parse_with_raw_status._weishushu_preserves_raw_status = True
    parser._parse_single_post = parse_with_raw_status
'线程级缓存（v1.1.2 强化）：每个线程一个 WeiboClient 实例。\n\n- v1.1.1 A1 修复：模块级全局 _cached_client → threading.local()\n- v1.1.2 强化：延迟初始化（lazy proxy）—— WeiboBook() 构造时不再立即启 Playwright\n  旧实现：create_weibo_client(None) 立即 WeiboClient() → import 即启 chromium\n  新实现：client = WeiboClientProxy() → 用时才 WeiboClient()（解决 75 测试中 5 个炸的问题）\n'


class _SingleAttemptWeiboClient(WeiboClient):
    """低强度归档专用客户端：底层每次调用只进行一次 HTTP 尝试。"""

    def _request(
        self,
        url,
        params,
        max_retries=1,
        use_proxy=True,
        headers=None,
    ):
        return super()._request(
            url,
            params,
            max_retries=1,
            use_proxy=use_proxy,
            headers=headers,
        )


class _WeiboClientProxy:
    """延迟初始化代理：访问任何属性时真启 WeiboClient。

    v1.1.2 之前 v1.1.1 的实现是「create_weibo_client(None) 立即 WeiboClient()」，
    后果是 `WeiboBook()` 构造即启 Playwright（9-12s 开销）+ 测试环境无 chromium 即炸。
    改为 proxy 后：构造零开销，用到才启。
    """

    def __init__(
        self,
        cookie_str: Optional[str] = None,
        *,
        client_class=None,
        client_options: dict | None = None,
    ) -> None:
        self._cookie_str = cookie_str
        self._client_class = client_class or WeiboClient
        self._client_options = dict(client_options or {})
        self._real: Optional['WeiboClient'] = None  # type: ignore[name-defined]

    def _ensure(self) -> 'WeiboClient':  # type: ignore[name-defined]
        if self._real is None:
            if self._cookie_str:
                # 有 cookie：注入后实例化（不同登录态不复用）
                client = self._client_class(
                    auto_fetch_cookies=False,
                    **self._client_options,
                )
                client.session.cookies.clear()
                client.session.headers.update({'Cookie': self._cookie_str, 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Referer': 'https://m.weibo.cn/'})
            else:
                client = self._client_class(**self._client_options)
            preserve_raw_status(client.parser)
            self._real = client
        return self._real

    def __getattr__(self, name):
        return getattr(self._ensure(), name)


def create_weibo_client(
    cookie_str: Optional[str] = None,
    *,
    low_intensity: bool = False,
):
    """创建 WeiboClient 实例（v1.1.2 延迟代理版）

    - 无 cookie → 复用线程级代理（首次访问才 WeiboClient()）
    - 有 cookie → 新代理（首次访问才 WeiboClient() 并注入 cookie）
    - 任何属性访问触发 _ensure() 真启 Playwright

    测试场景：`WeiboBook()` 构造时拿到 proxy，不真启 chromium（不调任何方法），
    调 .get_user_by_uid() 等 API 时才真启 → 测试可 mock WeiboClient 类替换 proxy 内部。
    """
    if low_intensity:
        if package_version("crawl4weibo") != "0.5.2":
            raise WeiboError(
                "低强度请求客户端仅支持 crawl4weibo 0.5.2",
                kind=WeiboErrorKind.API,
                recoverable=False,
            )
        return _WeiboClientProxy(
            cookie_str=cookie_str,
            client_class=_SingleAttemptWeiboClient,
            client_options={
                "rate_limit_config": RateLimitConfig(disable_delay=True),
            },
        )
    if not cookie_str:
        cached = getattr(_tls_cached_client, 'client', None)
        if cached is None:
            cached = _WeiboClientProxy(cookie_str=None)
            _tls_cached_client.client = cached
        return cached
    return _WeiboClientProxy(cookie_str=cookie_str)

def resolve_nickname(client: WeiboClient, nickname: str) -> str:
    """通过昵称解析 UID"""
    try:
        results = client.search_users(nickname, page=1, count=10)
        for user in results:
            if user.screen_name == nickname:
                return user.id
        if results:
            return results[0].id
    except Exception as e:
        raise ValueError(f"无法通过昵称 '{nickname}' 找到用户: {e}")
    raise ValueError(f"未找到昵称为 '{nickname}' 的用户")

class WeiboExtractor:
    """微博数据提取器"""

    def __init__(self, cookie_str: Optional[str]=None, image_quality: ImageQuality=ImageQuality.ORIGINAL, *, low_intensity: bool=False):
        self.client = create_weibo_client(
            cookie_str,
            low_intensity=low_intensity,
        )
        self.image_quality = image_quality
        self._user_info: Optional[UserInfo] = None
        self._has_cookies = bool(cookie_str)
        # L1 v1.1.1：跟踪最后一次提取的"是否部分失败"，供 WeiboBook.extract 包装
        self._last_partial: bool = False
        self._last_pages_failed: int = 0
        self._last_pages_total: int = 0
        self._last_partial_reason: str = ""
        self._cancel_event: Optional[threading.Event] = None
        self._progress_event_callback = None

    def _emit_progress_event(self, event: dict) -> None:
        callback = self._progress_event_callback
        if callback is not None:
            callback(event)

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled("任务已取消")

    def _wait_or_cancel(self, delay: float) -> None:
        if self._cancel_event is None:
            time.sleep(delay)
            return
        if self._cancel_event.wait(delay):
            self._check_cancelled()

    def resolve_url(self, url: str) -> str:
        """解析 URL，返回最终 UID"""
        uid_or_nick = parse_uid_from_url(url)
        if uid_or_nick.startswith('nickname:'):
            nickname = uid_or_nick.split(':', 1)[1]
            if self.client:
                return resolve_nickname(self.client, nickname)
            raise ValueError(f"需要登录才能通过昵称 '{nickname}' 查找用户")
        return uid_or_nick

    def get_user_info(self, uid: str) -> UserInfo:
        """获取用户信息

        v1.1.1 修复 L4：抛 WeiboError（带 kind）而不是 RuntimeError，
        前端能区分「认证」「限流」「未找到」等给友好提示。
        """
        try:
            user = self.client.get_user_by_uid(uid)
            self._user_info = UserInfo(uid=user.id, screen_name=user.screen_name, avatar_url=user.avatar_url, description=user.description, followers_count=user.followers_count, following_count=user.following_count, posts_count=user.posts_count, verified=user.verified, verified_reason=user.verified_reason, location=user.location, gender=user.gender, cover_image_url=user.cover_image_url)
            return self._user_info
        except Exception as e:
            err_msg = str(e)
            kind = classify_error(e)
            if kind == WeiboErrorKind.NOT_FOUND or 'not found' in err_msg.lower():
                hint = '\n💡 提示: 该用户可能设置了访问限制，请点击「扫码登录」后重试。'
            else:
                hint = ''
            raise WeiboError(
                f'获取用户信息失败: {err_msg}{hint}',
                kind=kind,
                original=e,
            )

    def preview_posts(self, uid: str, count: int=20) -> list[dict]:
        """快速预览用户最近的帖子（仅摘要，不含评论/媒体下载）

        Returns:
            list[dict]: [{bid, text, created_at, media_count, pic_url}, ...]
        """
        if not self._user_info:
            self.get_user_info(uid)
        # L1 + L3 v1.1.1：重置 partial 跟踪
        self._last_partial = False
        self._last_pages_failed = 0
        self._last_pages_total = 0
        self._last_partial_reason = ""
        previews = []
        page = 1
        while len(previews) < count:
            try:
                crawl_posts = self.client.get_user_posts(uid, page=page, expand=False, with_comments=False)
            except Exception as e:
                self._last_partial = True
                self._last_pages_failed += 1
                self._last_partial_reason = f'预览第{page}页失败: {e}'
                logger.warning(f'  ⚠️ {self._last_partial_reason}（已部分加载 {len(previews)} 条）')
                break
            if not crawl_posts:
                break
            self._last_pages_total += 1
            for cp in crawl_posts:
                previews.append({
                    'bid': cp.bid,
                    'text': (cp.text or '')[:120],
                    'created_at': str(cp.created_at) if cp.created_at else '',
                    'media_count': len(cp.pic_urls or []) + (1 if cp.video_url else 0),
                    'pic_url': (cp.pic_urls or [None])[0],
                    'is_original': cp.is_original,
                    'reposts_count': cp.reposts_count or 0,
                    'comments_count': cp.comments_count or 0,
                    'likes_count': cp.attitudes_count or 0,
                })
                if len(previews) >= count:
                    break
            page += 1
        return previews

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """解析日期字符串，返回带 UTC+8 时区信息的 datetime"""
        if not date_str:
            return None
        tz = timezone(timedelta(hours=8))
        for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S'):
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.replace(tzinfo=tz)
            except ValueError:
                continue
        return None

    def _parse_date_range(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        """返回 UTC+8 左闭右开边界；结束日覆盖当日全天。"""
        start_dt = self._parse_date(start_date)
        end_exclusive = self._parse_date(end_date)
        if end_exclusive is not None:
            end_exclusive += timedelta(days=1)
        return start_dt, end_exclusive

    def get_posts(self, uid: str, max_posts: int=0, comments: bool=False, comments_count: int=5, comments_type: str='hot', start_date: Optional[str]=None, end_date: Optional[str]=None, only_original: bool=False, post_ids: Optional[list[str]]=None) -> list[Post]:
        """获取用户的帖子列表

        若指定 post_ids，只提取这些 BID 的帖子；否则按正常流程分页提取。

        L1 v1.1.1：失败部分提取时，结果可能少于用户预期。
        调用方应在 WeiboExtractor 实例上读 `_last_partial` / `_last_pages_failed` / `_last_partial_reason`。
        """
        self._check_cancelled()
        if not self._user_info:
            self.get_user_info(uid)
        # 重置 partial 标记（每次调用都重新统计）
        self._last_partial = False
        self._last_pages_failed = 0
        self._last_pages_total = 0
        self._last_partial_reason = ""
        if post_ids is not None:
            return self._get_posts_by_ids(uid, post_ids, comments, comments_count, comments_type)
        posts = self._get_posts_api(uid, max_posts, comments, comments_count, comments_type, start_date, end_date, only_original)
        self._check_cancelled()
        if len(posts) == 0 and self._has_cookies:
            logger.info('  API 未返回数据，切换到浏览器模式提取...')
            posts = self._get_posts_browser(uid, max_posts, comments, comments_count, comments_type, start_date, end_date, only_original)
        return posts

    def _get_posts_api(self, uid, max_posts, comments, comments_count, comments_type, start_date, end_date, only_original):
        start_dt, end_exclusive = self._parse_date_range(start_date, end_date)
        our_posts = []
        page = 1
        total = 0
        label = '原创微博' if only_original else '微博'
        logger.info(f'正在提取 @{self._user_info.screen_name} 的{label}...')
        max_attempts = 3
        while True:
            self._check_cancelled()
            attempt = 0
            crawl_posts = None
            while attempt < max_attempts:
                self._check_cancelled()
                try:
                    crawl_posts = self.client.get_user_posts(
                        uid,
                        page=page,
                        expand=False,
                        with_comments=False,
                    )
                    for crawl_post in crawl_posts:
                        if not getattr(crawl_post, "is_long_text", False):
                            continue
                        self._check_cancelled()
                        try:
                            expanded = self.client.get_post_by_bid(
                                crawl_post.bid,
                                with_comments=False,
                            )
                        except OperationCancelled:
                            raise
                        except Exception as expand_error:
                            logger.warning(
                                "  长微博 %s 全文提取失败，保留列表页正文：%s",
                                crawl_post.bid,
                                expand_error,
                            )
                            continue
                        crawl_post.text = expanded.text
                        crawl_post.pic_urls = expanded.pic_urls
                        crawl_post.video_url = expanded.video_url
                        crawl_post.raw_data = expanded.raw_data
                    break
                except OperationCancelled:
                    raise
                except Exception as e:
                    attempt += 1
                    kind = classify_error(e)
                    if attempt >= max_attempts:
                        # L1 v1.1.1：标记 partial，不静默 return
                        self._last_partial = True
                        self._last_pages_failed += 1
                        self._last_partial_reason = f"第{page}页连续{max_attempts}次失败[{kind.value}]: {e}"
                        logger.warning(f'  ⚠️ {self._last_partial_reason}（已部分提取 {len(our_posts)} 条）')
                        return our_posts
                    if not is_recoverable(kind) and attempt > 0:
                        self._last_partial = True
                        self._last_pages_failed += 1
                        self._last_partial_reason = f"第{page}页失败[{kind.value}]: {e}（不可重试）"
                        logger.warning(f'  ⚠️ {self._last_partial_reason}（已部分提取 {len(our_posts)} 条）')
                        return our_posts
                    delay = get_retry_delay(attempt, base=2.0, max_delay=60.0)
                    logger.info(f'  第 {page} 页失败({attempt}/{max_attempts}) [{kind.value}]: {e}, {delay:.1f}s 后重试')
                    self._wait_or_cancel(delay)
            if not crawl_posts:
                break
            self._last_pages_total += 1
            for cp in crawl_posts:
                self._check_cancelled()
                pd = cp.created_at
                if pd and pd.tzinfo is None:
                    pd = pd.replace(tzinfo=timezone(timedelta(hours=8)))
                if start_dt and pd and (pd < start_dt):
                    continue
                if end_exclusive and pd and (pd >= end_exclusive):
                    continue
                if only_original and (not cp.is_original):
                    continue
                pc = None
                if comments:
                    try:
                        pc = self.get_post_comments(cp.bid, uid, comments_count, comments_type)
                    except Exception as ex:
                        logger.info(f'    评论提取失败: {ex}')
                op = crawl_post_to_our_post(cp, uid, pc, self.image_quality)
                op.user_name = self._user_info.screen_name
                op.user_avatar = self._user_info.avatar_url
                our_posts.append(op)
                total += 1
                if max_posts > 0 and total >= max_posts:
                    break
            logger.info(f'  第 {page} 页: {len(crawl_posts)} 条')
            self._emit_progress_event({
                "current_page": page,
                "current": len(our_posts),
                "total": max_posts if max_posts > 0 else None,
                "unit": "post",
                "detail": f"第 {page} 页 · 已处理 {len(our_posts)} 条",
            })
            if max_posts > 0 and total >= max_posts:
                break
            page += 1
            self._wait_or_cancel(0.5)
        if our_posts:
            logger.info(f'\n📊 共提取 {len(our_posts)} 条微博')
        return our_posts

    def _get_posts_by_ids(self, uid, post_ids, comments, comments_count, comments_type):
        our_posts = []
        total = len(post_ids)
        msg = f'正在提取 {total} 条指定微博...'
        logger.info(msg)
        for idx, bid in enumerate(post_ids, 1):
            self._check_cancelled()
            try:
                cp = self.client.get_post_by_bid(bid, with_comments=False)
            except OperationCancelled:
                raise
            except Exception as ex:
                self._last_partial = True
                self._last_pages_failed += 1
                self._last_partial_reason = f'指定微博 {bid} 正文提取失败: {ex}'
                logger.warning(f'  [{idx}/{total}] {self._last_partial_reason}')
                self._emit_progress_event({
                    "current_page": None, "current": idx, "total": total,
                    "unit": "post", "detail": f"指定微博 {idx}/{total}",
                })
                continue
            pc = None
            if comments:
                try:
                    pc = self.get_post_comments(cp.bid, uid, comments_count, comments_type)
                except OperationCancelled:
                    raise
                except Exception as ex:
                    self._last_partial = True
                    self._last_pages_failed += 1
                    self._last_partial_reason = f'指定微博 {bid} 评论提取失败: {ex}'
                    logger.warning(f'  [{idx}/{total}] {self._last_partial_reason}')
            op = crawl_post_to_our_post(cp, uid, pc, self.image_quality)
            op.user_name = self._user_info.screen_name
            op.user_avatar = self._user_info.avatar_url
            our_posts.append(op)
            self._emit_progress_event({
                "current_page": None, "current": idx, "total": total,
                "unit": "post", "detail": f"指定微博 {idx}/{total}",
            })
        logger.info(f'提取完成: {len(our_posts)}/{total} 条')
        return our_posts

    def _get_posts_browser(self, uid, max_posts, comments, comments_count, comments_type, start_date, end_date, only_original):
        """通过 Playwright 浏览器直接提取帖子（API 不可用时）"""
        start_dt, end_exclusive = self._parse_date_range(start_date, end_date)
        return fetch_posts_browser(
            self.client,
            user=self._user_info,
            uid=uid,
            max_posts=max_posts,
            start_dt=start_dt,
            end_dt=end_exclusive,
            only_original=only_original,
            has_cookies=self._has_cookies,
        )

    def get_favorites(self, uid: str, max_posts: int=0) -> list[Post]:
        """获取用户的收藏微博

        使用 m.weibo.cn 的收藏 API 直接提取。

        v1.1.1 修复 L2：失败时复用 3 次重试（与 _get_posts_api 一致），单页失败不静默
        v1.1.1 修复 L9：跳过的不合规收藏记录到 `_skipped_bids`

        Args:
            uid: 用户 UID（用于获取用户信息）
            max_posts: 最大提取条数（0=全部）

        Returns:
            list[Post]: 收藏的帖子列表
        """
        if not self._user_info:
            self.get_user_info(uid)
        fetch_result = fetch_favorites(
            self.client,
            uid=uid,
            screen_name=self._user_info.screen_name,
            max_posts=max_posts,
            image_quality=self.image_quality,
            post_converter=crawl_post_to_our_post,
        )
        self._last_partial = fetch_result.partial
        self._last_pages_failed = fetch_result.pages_failed
        self._last_pages_total = fetch_result.pages_total
        self._last_partial_reason = fetch_result.partial_reason
        self._skipped_bids = fetch_result.skipped_bids
        return fetch_result.posts

    def get_post_comments(self, post_id: str, blogger_uid: str, count: int=5, comments_type: str='hot') -> list[Comment]:
        """获取帖子的评论"""
        return fetch_post_comments(self.client, post_id, blogger_uid, count, comments_type)

    def get_extracted(self, uid: str, extract_type: ExtractType=ExtractType.POSTS, max_posts: int=0, comments: bool=False, comments_count: int=5, comments_type: str='hot', start_date: Optional[str]=None, end_date: Optional[str]=None, only_original: bool=False, post_ids: Optional[list[str]]=None) -> list[Post]:
        """统一入口：根据 extract_type 分发到 get_posts 或 get_favorites"""
        if extract_type == ExtractType.FAVORITES:
            return self.get_favorites(uid=uid, max_posts=max_posts)
        return self.get_posts(uid=uid, max_posts=max_posts, comments=comments, comments_count=comments_count, comments_type=comments_type, start_date=start_date, end_date=end_date, only_original=only_original, post_ids=post_ids)
