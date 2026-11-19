"""现有微博抓取与媒体组件的归档数据源适配器（本人或指定目标博主）。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from crawl4weibo.exceptions.base import CrawlError

from weibo_book.errors import OperationPaused, WeiboError, WeiboErrorKind, classify_error
from weibo_book.comment_fetcher import fetch_post_comments_strict
from weibo_book.extractor import WeiboExtractor
from weibo_book.media import MediaDownloader
from weibo_book.models import Comment, ImageQuality, MediaType, Post
from weibo_book.post_converter import crawl_post_to_our_post

from .discovery import ProfileItem, ProfilePage
from .avatar_album import (
    ALBUM_LIST_URL,
    MAX_PHOTO_PAGES,
    PHOTO_LIST_URL,
    PHOTO_PAGE_SIZE,
    parse_avatar_album_id,
    parse_photo_page,
)
from .live_replay import parse_live_id, parse_replay_payload
from .media_layout import local_path_v4, media_path_shape, media_year_month
from .schema import MediaRecord
from .pacing import AdaptiveRequestScheduler
from .sync import StagedMedia
from .video_playback import parse_best_playback_url, video_quality_score

logger = logging.getLogger(__name__)


def _upgrade_cover_url(url: object) -> str:
    """封面 URL 升级到 ``large`` 尺寸。

    用户接口返回的封面多为 ``crop.0.0.640.640.640`` 小图，横幅显示模糊。
    只处理无签名查询串的 sinaimg 链接；带 ``KID/Expires/ssig`` 签名的
    链接改写路径会失效，保持原样。
    """
    if not isinstance(url, str) or "sinaimg.cn/crop." not in url or "?" in url:
        return url if isinstance(url, str) else ""
    return re.sub(r"/crop\.[^/]+/", "/large/", url, count=1)


PROFILE_INFO_URL = "https://weibo.com/ajax/profile/info"


def _parse_cover_urls(payload: object) -> list[str]:
    """从 PC ``ajax/profile/info`` 响应解析全部主页封面。

    2026-09-08 实测：``data.user.cover_image_phone`` 为分号分隔的多张封面
    （微博支持多张轮换），逐张升级为 ``large`` 尺寸并按原顺序去重。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    user = data.get("user")
    if not isinstance(user, dict):
        return []
    raw = user.get("cover_image_phone")
    if not isinstance(raw, str) or not raw.strip():
        return []
    covers: list[str] = []
    for piece in raw.split(";"):
        piece = piece.strip()
        if not piece.startswith(("http://", "https://")):
            continue
        upgraded = _upgrade_cover_url(piece)
        if upgraded and upgraded not in covers:
            covers.append(upgraded)
    return covers


class WeiboArchiveSource:
    """将 ``WeiboExtractor`` 的已验证调用适配为归档数据源。"""

    def __init__(
        self,
        extractor: WeiboExtractor,
        *,
        self_uid: str,
        image_quality: ImageQuality,
        pacing_scheduler: AdaptiveRequestScheduler | None = None,
        target_uid: str | None = None,
    ) -> None:
        self.extractor = extractor
        self.self_uid = self_uid
        self.target_uid = target_uid
        self.image_quality = image_quality
        self.pacing_scheduler = pacing_scheduler
        self._verified_video_urls: dict[str, str] = {}

    def _require_self(self, uid: str) -> None:
        allowed = self.target_uid if self.target_uid is not None else self.self_uid
        if uid != allowed:
            raise WeiboError("归档数据源只允许访问归档目标账号", kind=WeiboErrorKind.AUTH)

    def probe_session(self) -> None:
        """唤醒后使用已确认的配置端点复检登录状态和本人 UID。"""

        try:
            response = self.extractor.client.session.get(
                "https://m.weibo.cn/api/config",
                timeout=5,
            )
            if response.status_code in {429, 432}:
                raise OperationPaused(
                    "请求受到限流，任务已暂停",
                    pause_reason="rate_limited",
                )
            if response.status_code in {401, 403}:
                raise OperationPaused(
                    "登录状态已失效，任务已暂停",
                    pause_reason="authentication_required",
                )
            response.raise_for_status()
        except OperationPaused:
            raise
        except Exception as exc:
            kind = classify_error(exc)
            if kind is WeiboErrorKind.AUTH:
                raise OperationPaused(
                    "登录状态已失效，任务已暂停",
                    pause_reason="authentication_required",
                ) from exc
            raise OperationPaused(
                "唤醒后网络复检失败，任务已暂停",
                pause_reason="network_unavailable",
            ) from exc
        try:
            payload = response.json()
        except Exception as exc:
            raise WeiboError(
                "唤醒后的会话复检响应无效",
                kind=WeiboErrorKind.PARSE,
                original=exc,
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or type(data.get("login")) is not bool:
            raise WeiboError(
                "唤醒后的会话复检响应无效",
                kind=WeiboErrorKind.PARSE,
            )
        if data["login"] is not True:
            raise OperationPaused(
                "登录状态已失效，任务已暂停",
                pause_reason="authentication_required",
            )
        uid = str(data.get("uid", "")).strip()
        if not uid:
            raise WeiboError(
                "唤醒后的会话复检缺少 UID",
                kind=WeiboErrorKind.PARSE,
            )
        if self.target_uid is None and uid != self.self_uid:
            raise OperationPaused(
                "当前登录账号与归档任务不一致，任务已暂停",
                pause_reason="account_mismatch",
            )

    def iter_profile_pages(
        self,
        uid: str,
        *,
        start_page: int = 1,
        pin_orders: dict[str, int] | None = None,
        next_pin_order: int = 1,
    ) -> Iterator[ProfilePage]:
        self._require_self(uid)
        if type(start_page) is not int or start_page < 1:
            raise WeiboError("主页起始页码无效", kind=WeiboErrorKind.API)
        if type(next_pin_order) is not int or next_pin_order < 1:
            raise WeiboError("置顶序号恢复点无效", kind=WeiboErrorKind.API)
        page_number = start_page
        pin_orders = dict(pin_orders or {})

        def fetch_page(number: int) -> list:
            def request_page():
                try:
                    return self.extractor.client.get_user_posts(
                        uid, page=number, expand=False, with_comments=False
                    )
                except CrawlError as exc:
                    raise WeiboError("读取本人主页失败", kind=classify_error(exc), original=exc) from exc

            return (
                self.pacing_scheduler.run("profile", request_page)
                if self.pacing_scheduler is not None
                else request_page()
            ) or []

        prefetched: list | None = None
        while True:
            if prefetched is not None:
                crawl_posts, prefetched = prefetched, None
            else:
                crawl_posts = fetch_page(page_number)
            if not crawl_posts:
                # crawl4weibo 在“整页没有可解析微博”和“业务异常缺少 data”时都返回空列表，
                # 与真正翻到底无法区分。再看下一页：仍为空才判定到底，否则只是中间空页。
                following = fetch_page(page_number + 1)
                if not following:
                    yield ProfilePage([], is_last=True)
                    return
                logger.warning("主页第 %d 页没有可解析的微博，后续仍有内容，继续翻页", page_number)
                yield ProfilePage([], is_last=False)
                page_number += 1
                prefetched = following
                continue
            items = []
            for crawl_post in crawl_posts:
                converted = crawl_post_to_our_post(
                    crawl_post, uid, image_quality=self.image_quality
                )
                pin_order = None
                if converted.is_pinned:
                    pin_order = pin_orders.get(converted.bid)
                    if pin_order is None:
                        pin_order = next_pin_order
                        pin_orders[converted.bid] = pin_order
                        next_pin_order += 1
                items.append(
                    ProfileItem(
                        converted.bid,
                        converted.is_pinned,
                        pin_order,
                        converted.created_at,
                    )
                )
            yield ProfilePage(items, is_last=False)
            page_number += 1

    def supertopic_source(self, uid: str):
        self._require_self(uid)
        from .supertopic import SupertopicSource
        return SupertopicSource(self.extractor.client.session, uid, self.pacing_scheduler)

    def fetch_post(self, uid: str, bid: str) -> Post:
        self._require_self(uid)
        def request_post():
            try:
                return self.extractor.client.get_post_by_bid(
                    bid, with_comments=False
                )
            except CrawlError as exc:
                raise WeiboError("读取微博详情失败", kind=classify_error(exc), original=exc) from exc

        crawl_post = (
            self.pacing_scheduler.run("detail", request_post)
            if self.pacing_scheduler is not None
            else request_post()
        )
        post = crawl_post_to_our_post(
            crawl_post, uid, image_quality=self.image_quality
        )
        return self._upgrade_video_playback(post)

    STATUS_SHOW_AJAX_URL = "https://weibo.com/ajax/statuses/show"

    def fetch_video_playback_url(self, bid: str) -> str | None:
        """从 PC ``ajax/statuses/show`` 读取 ``playback_list`` 最高画质直链。

        m.weibo.cn 只返回 480p，PC 端接口含 1080p/720p；没有播放列表时
        返回 ``None``。
        """
        session = self.extractor.client.session

        def get_show():
            try:
                response = session.get(
                    self.STATUS_SHOW_AJAX_URL,
                    params={"id": bid},
                    headers={"Referer": "https://weibo.com/"},
                    timeout=20,
                )
                if response.status_code in {429, 432}:
                    raise OperationPaused(
                        "请求受到限流，任务已暂停",
                        pause_reason="rate_limited",
                    )
                if response.status_code in {401, 403}:
                    raise OperationPaused(
                        "登录状态已失效，任务已暂停",
                        pause_reason="authentication_required",
                    )
                response.raise_for_status()
                return response.json()
            except (OperationPaused, WeiboError):
                raise
            except Exception as exc:
                raise WeiboError("读取视频高清地址失败", kind=classify_error(exc), original=exc) from exc

        payload = (
            self.pacing_scheduler.run("detail", get_show)
            if self.pacing_scheduler is not None
            else get_show()
        )
        return parse_best_playback_url(payload)

    def _upgrade_video_playback(self, post: Post) -> Post:
        """单视频帖用 PC 播放列表升级到更高清晰度；多视频混合帖不动。"""
        self._verified_video_urls.pop(post.bid, None)
        videos = [
            item
            for item in post.media
            if item.type is MediaType.VIDEO and item.url
        ]
        if len(videos) != 1:
            return post
        item = videos[0]
        try:
            candidate = self.fetch_video_playback_url(post.bid)
        except OperationPaused:
            raise
        except Exception as exc:
            logger.warning("读取微博 %s 高清视频地址失败，保留原清晰度: %s", post.bid, exc)
            return post
        if candidate and video_quality_score(candidate) > video_quality_score(item.url):
            item.url = candidate
        if candidate:
            self._verified_video_urls[post.bid] = item.url
        return post

    def verified_video_url(self, bid: str) -> str | None:
        return self._verified_video_urls.get(bid)

    LIVE_SHOW_URL = "https://weibo.com/l/!/2/wblive/room/show_pc_live.json"

    def fetch_live_replay(self, live_page_url: str) -> dict[str, str] | None:
        """读取微博直播回放信息（HLS 播放地址、封面、标题）。

        ``live_page_url`` 为直播卡片里的落地页链接；非微博直播页或该场
        直播没有回放时返回 ``None``。
        """
        live_id = parse_live_id(live_page_url)
        if live_id is None:
            return None
        session = self.extractor.client.session

        def get_live():
            try:
                response = session.get(
                    self.LIVE_SHOW_URL,
                    params={"live_id": live_id},
                    headers={"Referer": "https://weibo.com/"},
                    timeout=20,
                )
                if response.status_code in {429, 432}:
                    raise OperationPaused(
                        "请求受到限流，任务已暂停",
                        pause_reason="rate_limited",
                    )
                if response.status_code in {401, 403}:
                    raise OperationPaused(
                        "登录状态已失效，任务已暂停",
                        pause_reason="authentication_required",
                    )
                response.raise_for_status()
                return response.json()
            except (OperationPaused, WeiboError):
                raise
            except Exception as exc:
                raise WeiboError("读取直播回放失败", kind=classify_error(exc), original=exc) from exc

        payload = (
            self.pacing_scheduler.run("detail", get_live)
            if self.pacing_scheduler is not None
            else get_live()
        )
        return parse_replay_payload(payload)

    def fetch_recent_comments(
        self, post_id: str, limit: int = 10
    ) -> list[Comment]:
        if self.target_uid is not None:
            # 他人归档第一版不抓评论：热门博主评论量巨大，慢且易触发限流
            return []
        def request_comments():
            return fetch_post_comments_strict(
                self.extractor.client, post_id, self.self_uid,
                count=limit, comments_type="hot",
            )

        return (
            self.pacing_scheduler.run("comments", request_comments)
            if self.pacing_scheduler is not None
            else request_comments()
        )

    def fetch_profile_assets(self, uid: str) -> tuple[str, str, int | None]:
        """读取归档目标账号当前的头像、封面链接与粉丝数。"""
        self._require_self(uid)

        payload = self._fetch_profile_info(uid)
        data = payload.get("data") if isinstance(payload, dict) else None
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise WeiboError("用户资料响应无效", kind=WeiboErrorKind.PARSE)
        # 2026-09-08 实测：PC 返回精确整数，移动端为“1353.7万”等展示文字。
        avatar_url = user.get("avatar_hd", "")
        covers = _parse_cover_urls(payload)
        cover_image_url = covers[0] if covers else ""
        followers_count = user.get("followers_count")
        if (
            isinstance(followers_count, bool)
            or not isinstance(followers_count, int)
            or followers_count < 0
        ):
            followers_count = None
        return (
            avatar_url if isinstance(avatar_url, str) else "",
            cover_image_url if isinstance(cover_image_url, str) else "",
            followers_count,
        )

    def fetch_avatar_album(self, uid: str) -> list[tuple[str, str | None]]:
        """拉取归档目标「头像相册」的历史头像。

        返回 ``[(大图 URL, 上传时间 ISO 或 None)]``；账号没有头像相册时返回
        空列表。每次调用最多 3 个请求（相册列表 1 个 + 照片列表最多 2 页）。
        """
        self._require_self(uid)
        session = self.extractor.client.session

        def get_json(url: str, params: dict[str, object], error_message: str):
            try:
                response = session.get(url, params=params, timeout=20)
                if response.status_code in {429, 432}:
                    raise OperationPaused(
                        "请求受到限流，任务已暂停",
                        pause_reason="rate_limited",
                    )
                if response.status_code in {401, 403}:
                    raise OperationPaused(
                        "登录状态已失效，任务已暂停",
                        pause_reason="authentication_required",
                    )
                response.raise_for_status()
                return response.json()
            except (OperationPaused, WeiboError):
                raise
            except Exception as exc:
                raise WeiboError(error_message, kind=classify_error(exc), original=exc) from exc

        def run_request(url: str, params: dict[str, object], error_message: str):
            if self.pacing_scheduler is not None:
                return self.pacing_scheduler.run(
                    "profile", lambda: get_json(url, params, error_message)
                )
            return get_json(url, params, error_message)

        album_payload = run_request(
            ALBUM_LIST_URL,
            {"uid": uid, "page": 1, "count": 30},
            "读取头像相册列表失败",
        )
        album_id = parse_avatar_album_id(album_payload)
        if album_id is None:
            return []

        photos: list[tuple[str, str | None]] = []
        total = 0
        for page in range(1, MAX_PHOTO_PAGES + 1):
            payload = run_request(
                PHOTO_LIST_URL,
                {"uid": uid, "album_id": album_id, "page": page, "count": PHOTO_PAGE_SIZE},
                "读取头像相册照片失败",
            )
            page_photos, total = parse_photo_page(payload)
            photos.extend(page_photos)
            if len(page_photos) < PHOTO_PAGE_SIZE or len(photos) >= total:
                break
        return photos

    def fetch_cover_list(self, uid: str) -> list[str]:
        """读取归档目标账号当前的全部主页封面（微博支持多张轮换）。

        返回 ``[large 封面 URL]``；读取失败按 ``WeiboError`` 抛出，由同步器
        降级为只记录当前封面。每次调用 1 个请求。
        """
        return _parse_cover_urls(self._fetch_profile_info(uid))

    def _fetch_profile_info(self, uid: str) -> object:
        self._require_self(uid)
        session = self.extractor.client.session

        def get_info():
            try:
                response = session.get(
                    PROFILE_INFO_URL,
                    params={"uid": uid},
                    headers={"Referer": "https://weibo.com/"},
                    timeout=20,
                )
                if response.status_code in {429, 432}:
                    raise OperationPaused(
                        "请求受到限流，任务已暂停",
                        pause_reason="rate_limited",
                    )
                if response.status_code in {401, 403}:
                    raise OperationPaused(
                        "登录状态已失效，任务已暂停",
                        pause_reason="authentication_required",
                    )
                response.raise_for_status()
                return response.json()
            except (OperationPaused, WeiboError):
                raise
            except Exception as exc:
                raise WeiboError("读取封面列表失败", kind=classify_error(exc), original=exc) from exc

        payload = (
            self.pacing_scheduler.run("profile", get_info)
            if self.pacing_scheduler is not None
            else get_info()
        )
        return payload


class _CancelSignal:
    def __init__(self, requested: Callable[[], bool]) -> None:
        self._requested = requested

    def is_set(self) -> bool:
        return self._requested()


class ArchiveMediaStager:
    """仅向同步器交付的 ``.work/<run>`` 写媒体。"""

    def __init__(
        self,
        *,
        image_quality: ImageQuality,
        downloader_factory: Callable[..., Any] = MediaDownloader,
        pacing_scheduler: AdaptiveRequestScheduler | None = None,
    ) -> None:
        self.image_quality = image_quality
        self.downloader_factory = downloader_factory
        self.pacing_scheduler = pacing_scheduler

    @staticmethod
    def _digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    @staticmethod
    def _relative_staged(work_root: Path, value: str | Path) -> tuple[Path, str]:
        staged = Path(value)
        if not staged.is_absolute():
            staged = work_root / staged
        try:
            relative = staged.resolve(strict=True).relative_to(
                work_root.resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise WeiboError("媒体暂存路径超出同步工作目录", kind=WeiboErrorKind.API, original=exc) from exc
        if not relative.parts or relative.parts[0] != "media":
            raise WeiboError("媒体暂存路径必须位于 media 目录", kind=WeiboErrorKind.API)
        return staged, relative.as_posix()

    @classmethod
    def _content_address_post_media(
        cls,
        work_root: Path,
        value: str | Path,
        *,
        bid: str,
        role: str,
        position: int,
        year_month: tuple[str, str],
    ) -> tuple[Path, str, str]:
        source, _ = cls._relative_staged(work_root, value)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", bid) or not re.fullmatch(r"[a-z_]+", role):
            raise WeiboError("微博媒体标识不安全", kind=WeiboErrorKind.API, recoverable=False)
        digest = cls._digest(source)
        suffix = source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            raise WeiboError("微博媒体扩展名不安全", kind=WeiboErrorKind.API, recoverable=False)
        directory = work_root / "media" / "posts" / year_month[0] / year_month[1]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{bid}_{role}_{position}_{digest}{suffix}"
        if source != target:
            if target.exists():
                marker = os.lstat(target)
                if (
                    marker.st_nlink != 1
                    or not stat.S_ISREG(marker.st_mode)
                    or cls._digest(target) != digest
                ):
                    raise WeiboError("微博媒体内容寻址文件冲突", kind=WeiboErrorKind.API, recoverable=False)
                source.unlink()
            else:
                os.replace(source, target)
        return target, target.relative_to(work_root).as_posix(), digest

    def stage(
        self,
        post: Post,
        comments: list[Comment],
        work_root: Path,
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> list[StagedMedia]:
        work_root.mkdir(parents=True, exist_ok=True)
        post.comments = comments
        downloader_options: dict[str, object] = {"image_quality": self.image_quality}
        if self.pacing_scheduler is not None:
            downloader_options["pacing_scheduler"] = self.pacing_scheduler
            if self.pacing_scheduler.is_low_intensity:
                downloader_options["max_workers"] = 1
        downloader = self.downloader_factory(work_root, **downloader_options)
        if cancel_requested is not None:
            downloader._cancel_event = _CancelSignal(cancel_requested)
        if progress_callback is not None:
            downloader._progress_event_callback = progress_callback
        downloader.download_all([post])

        staged: list[StagedMedia] = []

        def append(
            owner_type: str,
            owner_id: str,
            role: str,
            position: int,
            remote_url: str,
            value: str | Path | None,
            *,
            content_addressed: bool = False,
            year_month: tuple[str, str] | None = None,
        ) -> str | None:
            if not value:
                return None
            if content_addressed:
                if year_month is None:
                    raise WeiboError(
                        "微博缺少发布时间，无法归档媒体",
                        kind=WeiboErrorKind.API,
                        recoverable=False,
                    )
                path, relative, digest = self._content_address_post_media(
                    work_root, value, bid=owner_id, role=role, position=position,
                    year_month=year_month,
                )
            else:
                path, relative = self._relative_staged(work_root, value)
                digest = self._digest(path)
                parts = relative.split("/")
                if len(parts) == 3 and parts[1] == "comments":
                    if year_month is None:
                        raise WeiboError(
                            "微博缺少发布时间，无法归档评论图片",
                            kind=WeiboErrorKind.API,
                            recoverable=False,
                        )
                    directory = (
                        work_root / "media" / "comments" / year_month[0] / year_month[1]
                    )
                    directory.mkdir(parents=True, exist_ok=True)
                    target = directory / parts[2]
                    if path != target:
                        if target.exists():
                            marker = os.lstat(target)
                            if (
                                marker.st_nlink != 1
                                or not stat.S_ISREG(marker.st_mode)
                                or self._digest(target) != digest
                            ):
                                raise WeiboError(
                                    "评论图片归档文件冲突",
                                    kind=WeiboErrorKind.API,
                                    recoverable=False,
                                )
                            path.unlink()
                        else:
                            os.replace(path, target)
                        path = target
                        relative = path.relative_to(work_root).as_posix()
            if media_path_shape(relative) is None:
                raise WeiboError(
                    "媒体归档路径形状不安全",
                    kind=WeiboErrorKind.API,
                    recoverable=False,
                )
            archived_relative = local_path_v4(relative)
            if media_path_shape(archived_relative) is None:
                raise WeiboError(
                    "媒体归档路径形状不安全",
                    kind=WeiboErrorKind.API,
                    recoverable=False,
                )
            staged.append(StagedMedia(
                MediaRecord(
                    owner_type, owner_id, role, position, remote_url,
                    archived_relative, digest,
                ),
                path,
            ))
            return archived_relative

        def append_post_media(value: Post) -> None:
            year_month = media_year_month(value.created_at)
            for position, media in enumerate(value.media):
                media.local_path = append(
                    "post", value.bid, media.type.value, position,
                    media.url, media.local_path, content_addressed=True,
                    year_month=year_month,
                )
                media.local_thumb = append(
                    "post", value.bid, f"{media.type.value}_thumbnail", position,
                    media.thumbnail or "", media.local_thumb, content_addressed=True,
                    year_month=year_month,
                )
                media.video_cover = append(
                    "post", value.bid, "video_cover", position,
                    media.thumbnail or "", media.video_cover, content_addressed=True,
                    year_month=year_month,
                )
            if value.link_card is not None:
                value.link_card.local_image = append(
                    "post", value.bid, "link_card", 0,
                    value.link_card.image_url, value.link_card.local_image,
                    content_addressed=True, year_month=year_month,
                )
            if value.retweeted is not None:
                append_post_media(value.retweeted)

        append_post_media(post)

        avatar_inputs: list[tuple[str, str, str]] = []
        if post.user_avatar:
            avatar_inputs.append(("user", post.uid, post.user_avatar))
        if post.retweeted is not None and post.retweeted.user_avatar:
            avatar_inputs.append(("retweeted_user", post.retweeted.uid, post.retweeted.user_avatar))

        comment_year_month = media_year_month(post.created_at)
        comment_position = 0
        stack = list(reversed(comments))
        while stack:
            comment = stack.pop()
            stack.extend(reversed(comment.replies))
            if comment.user_avatar:
                avatar_inputs.append((
                    "comment", comment.user_id or comment.id, comment.user_avatar
                ))
            comment.local_image = append(
                "comment", comment.id, "image", comment_position,
                comment.image_url, comment.local_image,
                year_month=comment_year_month,
            )
            comment_position += 1
        seen_avatars: set[tuple[str, str]] = set()
        for owner_type, owner_id, remote_url in avatar_inputs:
            identity = (owner_type, owner_id)
            if identity in seen_avatars:
                continue
            seen_avatars.add(identity)
            avatar_path = downloader.download_avatar(remote_url, f"{owner_type}:{owner_id}")
            append(owner_type, owner_id, "avatar", 0, remote_url, avatar_path)
        return staged
