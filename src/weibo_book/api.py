"""微博书 - 主 API 类

提供完整的微博数据提取、媒体下载和书生成功能。
前端/客户端可以直接 import 使用此类。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from .extractor import WeiboExtractor
from .errors import OperationCancelled
from .generator import BookGenerator
from .login import (
    login_with_qrcode,
    cookies_to_header,
    cookies_to_header_for_host,
    load_cookies,
)
from .media import MediaDownloader
from .models import (
    CommentType,
    ExtractType,
    ImageQuality,
    Post,
    UserInfo,
)
from .reports import write_run_report

logger = logging.getLogger(__name__)


class WeiboBook:
    """微博书 - 主类

    用法:
        book = WeiboBook()
        result = book.generate("https://weibo.com/u/XXXXX")
    """

    def __init__(
        self,
        cookie_str: Optional[str] = None,
        cookie_file: Optional[str] = None,
        image_quality: ImageQuality = ImageQuality.ORIGINAL,
    ):
        self.cookie_str = cookie_str
        self.cookie_file = cookie_file
        self.image_quality = image_quality
        self._cancel_event: Optional[threading.Event] = None
        self._progress_event_callback: Optional[Callable[[dict], None]] = None
        self._progress_started_at: Optional[datetime] = None

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled("任务已取消")

    def _emit_progress_event(
        self,
        phase: str,
        pct: float,
        detail: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        unit: Optional[str] = None,
        **extra,
    ) -> None:
        callback = self._progress_event_callback
        if callback is None:
            return
        started_at = self._progress_started_at or datetime.now()
        event = {
            "phase": phase,
            "pct": pct,
            "detail": detail,
            "current": current,
            "total": total,
            "unit": unit,
            "elapsed_seconds": max(0.0, round((datetime.now() - started_at).total_seconds(), 1)),
            **extra,
        }
        callback(event)

    def ensure_login(self, force: bool = False) -> Optional[str]:
        """确保登录状态，返回 Cookie 字符串"""
        if self.cookie_str and not force:
            logger.info("🔑 使用提供的 Cookie")
            return self.cookie_str

        if not force:
            stored = load_cookies(self.cookie_file)
            if isinstance(stored, list) and stored:
                logger.info("🔑 使用缓存的 Cookie")
                return cookies_to_header_for_host(stored, "m.weibo.cn")
            if isinstance(stored, dict) and stored:
                cookie_list = stored.get("cookies")
                if isinstance(cookie_list, list) and cookie_list:
                    logger.info("🔑 使用缓存的 Cookie")
                    return cookies_to_header_for_host(cookie_list, "m.weibo.cn")
                logger.info("🔑 使用缓存的 Cookie")
                return "; ".join(f"{k}={v}" for k, v in stored.items())

            logger.info("未发现缓存 Cookie，将使用匿名模式")
            return None

        logger.info("📱 需要微博登录以获取完整内容")
        cookies = login_with_qrcode(
            cookie_file=self.cookie_file,
            login_timeout=120,
            headless=False,
        )
        if cookies:
            return cookies_to_header_for_host(cookies, "m.weibo.cn")
        logger.warning("⚠️  未登录，将使用匿名模式（部分内容可能不可见）")
        return None

    def extract(
        self,
        url: str,
        max_posts: int = 0,
        comments: bool = False,
        comments_count: int = 5,
        comments_type: str = "hot",
        login: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        only_original: bool = False,
        extract_type: ExtractType = ExtractType.POSTS,
        post_ids: Optional[list[str]] = None,
    ) -> dict:
        """提取微博数据

        Args:
            url: 微博主页 URL
            max_posts: 最大提取条数（0=全部）
            comments: 是否提取评论
            comments_count: 提取评论条数
            comments_type: 评论类型 (hot/blogger/all)
            login: 是否显式触发扫码（False 时只使用缓存 Cookie）
            start_date: 起始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            only_original: 仅原创微博
            extract_type: 提取类型（posts/favorites）

        Returns:
            dict: {"user": UserInfo, "posts": list[Post]}
        """
        self._check_cancelled()
        self._emit_progress_event("identify", 0.02, "正在识别账号")
        cookie_str = self.ensure_login(force=login)
        self._check_cancelled()

        extractor = WeiboExtractor(
            cookie_str=cookie_str,
            image_quality=self.image_quality,
        )
        extractor._cancel_event = self._cancel_event
        extractor._progress_event_callback = lambda event: self._emit_progress_event(
            "extract", 0.25, event.get("detail", "正在抓取微博"),
            current=event.get("current"), total=event.get("total"),
            unit=event.get("unit"), current_page=event.get("current_page"),
        )
        uid = extractor.resolve_url(url)
        self._check_cancelled()
        user = extractor.get_user_info(uid)
        self._check_cancelled()
        self._emit_progress_event("identify", 0.08, f"已识别 @{user.screen_name}", current=1, total=1, unit="account")

        type_name = "收藏" if extract_type == ExtractType.FAVORITES else "微博"
        logger.info("👤 %s (%s)", user.screen_name, user.uid)
        logger.info("   📊 %s 条%s · %s 粉丝", user.posts_count, type_name, user.followers_count)

        posts = extractor.get_extracted(
            uid=uid,
            extract_type=extract_type,
            max_posts=max_posts,
            comments=comments,
            comments_count=comments_count,
            comments_type=comments_type,
            start_date=start_date,
            end_date=end_date,
            only_original=only_original,
            post_ids=post_ids,
        )
        self._check_cancelled()

        # L1 v1.1.1：包装 partial 元信息，前端能区分"全部完成 / 部分完成 / 中途失败"
        return {
            "user": user,
            "posts": posts,
            "_partial": extractor._last_partial,
            "_pages_failed": extractor._last_pages_failed,
            "_pages_total": extractor._last_pages_total,
            "_partial_reason": extractor._last_partial_reason,
        }

    def preview_posts(self, url: str, count: int = 20) -> dict:
        """快速预览用户最近的帖子"""
        cookie_str = self.ensure_login(force=False)
        extractor = WeiboExtractor(
            cookie_str=cookie_str,
            image_quality=self.image_quality,
        )
        uid = extractor.resolve_url(url)
        user = extractor.get_user_info(uid)
        previews = extractor.preview_posts(uid, count)
        return {
            "user": user,
            "previews": previews,
            "_partial": extractor._last_partial,
            "_partial_reason": extractor._last_partial_reason,
        }

    def download_media(
        self,
        posts: list[Post],
        output_dir: str | Path,
    ) -> dict:
        """下载帖子中的媒体文件"""
        self._check_cancelled()
        downloader = MediaDownloader(
            output_dir,
            image_quality=self.image_quality,
        )
        downloader._cancel_event = self._cancel_event
        downloader._progress_event_callback = lambda event: self._emit_progress_event(
            "media", 0.65, event.get("detail", "正在下载媒体"),
            current=event.get("current"), total=event.get("total"), unit="media",
        )
        result = downloader.download_all(posts)
        self._check_cancelled()
        return result

    def generate_markdown(
        self,
        posts: list[Post],
        user: UserInfo,
        output_dir: str | Path,
    ) -> str:
        gen = BookGenerator(output_dir)
        return gen.generate_markdown(posts, user)

    def generate_pdf(
        self,
        posts: list[Post],
        user: UserInfo,
        output_dir: str | Path,
    ) -> str:
        gen = BookGenerator(output_dir)
        return gen.generate_pdf(posts, user)

    def generate(
        self,
        url: str,
        max_posts: int = 0,
        output_dir: str = "./output",
        formats: Optional[list[str]] = None,
        comments: bool = False,
        comments_count: int = 5,
        comments_type: str = "hot",
        download_media: bool = True,
        login: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        only_original: bool = False,
        extract_type: ExtractType = ExtractType.POSTS,
        post_ids: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> dict:
        """一键生成微博书

        Args:
            url: 微博主页 URL
            ...
            extract_type: 提取类型
            progress_callback: 进度回调 (0~1, 状态描述)

        Returns:
            dict: 包含生成结果
        """
        self._check_cancelled()
        if formats is None:
            formats = ["md", "pdf"]

        def _progress(pct: float, msg: str = ""):
            if progress_callback:
                progress_callback(pct, msg)

        start_time = datetime.now()
        self._progress_started_at = start_time
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        report_params = {
            "max_posts": max_posts,
            "formats": formats,
            "comments": comments,
            "comments_count": comments_count,
            "comments_type": comments_type,
            "download_media": download_media,
            "login": login,
            "start_date": start_date,
            "end_date": end_date,
            "only_original": only_original,
            "extract_type": extract_type.value,
            "post_ids": post_ids or [],
        }
        result = {
            "user": None,
            "posts": [],
            "posts_count": 0,
            "output_dir": str(output_path),
            "markdown": None,
            "pdf": None,
            "html": None,
            "json": None,
            "csv": None,
            "media_summary": {"total": 0, "success": 0, "fail": 0, "failed": []},
            "report": None,
        }

        _progress(0.01, "开始生成...")
        self._emit_progress_event("identify", 0.01, "准备识别账号")

        logger.info("=" * 60)
        logger.info("  微博书 - 开始生成")
        logger.info("=" * 60)

        try:
            data = self.extract(
                url=url,
                max_posts=max_posts,
                comments=comments,
                comments_count=comments_count,
                comments_type=comments_type,
                login=login,
                start_date=start_date,
                end_date=end_date,
                only_original=only_original,
                extract_type=extract_type,
                post_ids=post_ids,
            )
            user = data["user"]
            posts = data["posts"]
            self._check_cancelled()
            _progress(0.50, f"已提取 {len(posts)} 条")
            self._emit_progress_event(
                "extract", 0.50, f"已抓取 {len(posts)} 条微博",
                current=len(posts), total=len(posts), unit="post",
            )

            safe_name = user.screen_name.strip().replace("/", "_").replace("\\", "_")
            if not safe_name:
                safe_name = "微博书"
            type_tag = "收藏" if extract_type == ExtractType.FAVORITES else ""
            book_dir = output_path / f"{safe_name}{type_tag}_{start_time:%Y%m%d_%H%M%S}"
            book_dir.mkdir(parents=True, exist_ok=True)

            result.update({
                "user": user,
                "posts": posts,
                "posts_count": len(posts),
                "output_dir": str(book_dir),
            })

            if not posts:
                _progress(1.0, "无内容")
                self._emit_progress_event("report", 0.90, "正在写入运行报告", current=0, total=1, unit="file")
                result["report"] = write_run_report(
                    output_dir=book_dir,
                    started_at=start_time,
                    finished_at=datetime.now(),
                    url=url,
                    params=report_params,
                    result=result,
                )
                self._emit_progress_event("complete", 1.0, "已完成，无可保存微博", current=0, total=0, unit="post")
                return result

            if download_media:
                self._check_cancelled()
                _progress(0.55, "下载媒体文件...")
                self._emit_progress_event("media", 0.55, "正在统计媒体文件", current=0, total=None, unit="media")
                result["media_summary"] = self.download_media(posts, book_dir)
                self._check_cancelled()
                _progress(0.75, "媒体下载完成")
                media_summary = result["media_summary"]
                self._emit_progress_event(
                    "media", 0.75, "媒体下载完成",
                    current=media_summary.get("success", 0) + media_summary.get("fail", 0),
                    total=media_summary.get("total", 0), unit="media",
                )
            else:
                self._emit_progress_event("media", 0.75, "已跳过媒体下载", current=0, total=0, unit="media")

            self._check_cancelled()
            _progress(0.80, "生成输出文件中...")
            gen = BookGenerator(book_dir)

            format_total = len(formats)
            for format_index, fmt in enumerate(formats, 1):
                self._check_cancelled()
                fmt = fmt.lower()
                self._emit_progress_event(
                    "generate", 0.80, f"正在生成 {fmt.upper()} 文件",
                    current=format_index, total=format_total, unit="file",
                )
                if fmt in ("md", "markdown"):
                    result["markdown"] = gen.generate_markdown(posts, user)
                elif fmt == "pdf":
                    result["pdf"] = gen.generate_pdf(posts, user)
                elif fmt == "html":
                    result["html"] = gen.generate_html(posts, user)
                elif fmt == "json":
                    result["json"] = gen.generate_json(posts, user)
                elif fmt == "csv":
                    result["csv"] = gen.generate_csv(posts, user)

            self._emit_progress_event("report", 0.92, "正在写入运行报告", current=0, total=1, unit="file")
            result["report"] = write_run_report(
                output_dir=book_dir,
                started_at=start_time,
                finished_at=datetime.now(),
                url=url,
                params=report_params,
                result=result,
            )
            self._emit_progress_event("report", 0.96, "运行报告已写入", current=1, total=1, unit="file")

            _progress(1.0, "生成完成 ✓")
            self._emit_progress_event("complete", 1.0, "备份已完成", current=len(posts), total=len(posts), unit="post")
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info("=" * 60)
            logger.info("  ✅ 微博书生成完成！")
            logger.info("  📊 共 %d 条", len(posts))
            logger.info("  📁 %s", book_dir)
            logger.info("  🧾 报告 %s", result["report"])
            logger.info("  ⏱️  耗时 %.1f 秒", elapsed)
            logger.info("=" * 60)

            return result
        except OperationCancelled:
            logger.info("微博书生成已取消，不再继续写入输出文件")
            raise
        except Exception as exc:
            failed_dir = output_path / f"失败_{start_time:%Y%m%d_%H%M%S}"
            result["output_dir"] = str(failed_dir)
            result["report"] = write_run_report(
                output_dir=failed_dir,
                started_at=start_time,
                finished_at=datetime.now(),
                url=url,
                params=report_params,
                result=result,
                error=str(exc),
            )
            logger.error("❌ 生成失败，报告已写入: %s", result["report"])
            raise WeiboError(
                WeiboErrorKind.UNKNOWN,
                f"{exc}\n失败报告: {result['report']}",
            ) from exc
