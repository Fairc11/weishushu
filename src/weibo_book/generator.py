"""微博书 - 渲染生成模块

生成 Markdown 和 PDF 格式的"微博书"输出。
"""

from __future__ import annotations

import csv
import html
import json
import logging
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# 显式导入，确保 PyInstaller 正确打包
import jinja2  # noqa: F401
import playwright  # noqa: F401
from jinja2 import Environment, FileSystemLoader

from .models import MediaType, Post, UserInfo

logger = logging.getLogger(__name__)


def _markdown_text(value: object) -> str:
    """把外部文本变成 Markdown 纯文本，不允许 HTML 或链接语法注入。"""
    escaped = html.escape(value if isinstance(value, str) else "", quote=False)
    escaped = escaped.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "(", ")", "#", "!", "|", ">"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _markdown_text_lines(value: object, prefix: str = "") -> list[str]:
    """转义 Markdown 文本，并为每个保留行添加结构前缀。"""
    return [f"{prefix}{line}" for line in _markdown_text(value).split("\n")]


def _markdown_url(value: str) -> str:
    """只为生成器明确创建的链接编码会破坏 Markdown 结构的字符。"""
    return quote(value, safe="/:?&=#%+-._~")


def _append_archive_media_markdown(
    lines: list[str], media_items, post_anchor: str, namespace: str, label: str,
) -> None:
    for media in media_items:
        kind = media["kind"]
        if kind == "image":
            original = media["browser_url"]
            thumbnail = media.get("thumbnail_url") or original
            lines.extend([
                f"[![{label}]({thumbnail})]({original})",
                "",
            ])
        elif kind == "video":
            video = media["browser_url"]
            cover = media.get("cover_url")
            media_label = "视频" if namespace == "main" else "转发视频"
            if cover:
                lines.append(f"[![{media_label}封面]({cover})]({video})")
            lines.extend([
                f"[播放{media_label}]({video})",
                "",
            ])
        elif kind == "live_photo":
            image = media["image_url"]
            video = media["video_url"]
            media_label = "实况照片" if namespace == "main" else "转发实况照片"
            lines.extend([
                f"[![{media_label}]({image})]({video})",
                f"[播放{media_label}视频]({video})",
                "",
            ])


def _append_archive_link_card_markdown(
    lines: list[str], card, post_anchor: str, namespace: str, prefix: str = "",
) -> None:
    if not card:
        return
    if card.get("browser_url"):
        local = card["browser_url"]
        lines.append(f"{prefix}[![引用卡片图片]({local})]({local})")
    title = _markdown_text(card.get("title") or "引用卡片")
    if card.get("url"):
        lines.append(f"{prefix}[🔗 {title}]({_markdown_url(card['url'])})")
    elif title:
        lines.append(f"{prefix}🔗 {title}")
    if card.get("description"):
        lines.append(f"{prefix}{_markdown_text(card['description'])}")
    lines.append("")


def render_archive_markdown(snapshot) -> str:
    """生成可随整个归档目录搬移的 Markdown 降级版。

    这里只使用已经过归档快照安全投影的相对路径，不向 Markdown
    写入临时渲染目录或归档根目录的绝对路径。
    """
    user = snapshot.user
    posts = snapshot.posts
    screen_name = _markdown_text(user["screen_name"])
    lines = [f"# {screen_name} 的微博书", ""]
    if user.get("avatar_url"):
        lines.extend([f"![{screen_name}头像]({user['avatar_url']})", ""])
    lines.append(f"- 微博 UID：{_markdown_text(user.get('uid'))}")
    lines.append(f"- 收录微博：{len(posts)} 条")
    dated = [post["created_at"] for post in posts if post["created_at"]]
    if dated:
        lines.append(
            f"- 时间跨度：{_markdown_text(min(dated))} 至 {_markdown_text(max(dated))}"
        )
    if user.get("created_at"):
        lines.append(f"- 归档创建：{_markdown_text(user['created_at'])}")
    if user.get("last_successful_sync_at"):
        lines.append(f"- 最近同步：{_markdown_text(user['last_successful_sync_at'])}")
    lines.append("")
    months = list(snapshot.timeline.get("months") or [])
    if months:
        lines.extend(["## 目录", ""])
        for month in months:
            lines.append(
                f"- {month['year']} 年 {month['month']} 月"
                f"（{month['end'] - month['start']} 条）"
            )
        lines.append("")
    month_by_start = {month["start"]: month for month in months}
    normal_index = -1
    current_year = None
    for post in posts:
        if post["is_pinned"] is not True:
            normal_index += 1
            month = month_by_start.get(normal_index)
            if month is not None:
                if month["year"] != current_year:
                    current_year = month["year"]
                    lines.extend([f"## {current_year} 年", ""])
                lines.extend([f"### {month['year']} 年 {month['month']} 月", ""])
        bid = post["bid"]
        encoded_bid = quote(bid, safe="-._~")
        post_anchor = f"微博书.html#post-{encoded_bid}"
        pin_label = " · 置顶" if post["is_pinned"] else ""
        unavailable_label = " · 不可见" if post["visibility"] != "visible" else ""
        lines.extend([
            f"#### {screen_name}{pin_label}{unavailable_label}",
            "",
            " · ".join(_markdown_text(value) for value in (
                post["created_at"], post["source"], post["ip_location"]
            ) if value),
            "",
        ])
        lines.extend(_markdown_text_lines(post["text"]))
        lines.extend([
            "",
            f"[在互动微博书中查看]({post_anchor})",
            "",
        ])
        _append_archive_media_markdown(lines, post["media"], post_anchor, "main", "图片")
        _append_archive_link_card_markdown(lines, post["link_card_payload"], post_anchor, "link")
        retweet = post["retweeted_payload"]
        if retweet:
            if retweet.get("avatar_url"):
                avatar = retweet["avatar_url"]
                lines.append(f"[![{_markdown_text(retweet['user_name'] or '原博主')}头像]({avatar})]({avatar})")
            lines.extend([
                f"> **转发自 @{_markdown_text(retweet['user_name'] or '原博主')}**",
                f"> {' · '.join(_markdown_text(value) for value in (retweet['created_at'], retweet['source'], retweet['ip_location']) if value)}",
            ])
            lines.extend(_markdown_text_lines(retweet["text"], prefix="> "))
            lines.append("")
            _append_archive_media_markdown(lines, retweet["media"], post_anchor, "retweet", "转发媒体")
            _append_archive_link_card_markdown(lines, retweet["link_card"], post_anchor, "retweet-link", "> ")
        lines.extend([
            f"转发 {post['reposts_count']} · 评论 {post['comments_count']} · 赞 {post['likes_count']}",
            "",
        ])
        if post["comments"]:
            lines.extend(["##### 评论", ""])
        for comment in post["comments"]:
            indent = "  " if comment["parent_id"] else ""
            reply = f" · 回复 @{_markdown_text(comment['reply_to_name'])}" if comment["reply_to_name"] else ""
            blogger = " · 博主" if comment["is_blogger"] else ""
            meta = " · ".join(_markdown_text(value) for value in (
                comment["created_at"], comment["source"]
            ) if value)
            lines.append(
                f"{indent}- **{_markdown_text(comment['user_name'] or '微博用户')}**"
                f"{blogger}{reply}"
            )
            if comment.get("avatar_url"):
                avatar = comment["avatar_url"]
                lines.append(f"{indent}  [![{_markdown_text(comment['user_name'] or '微博用户')}头像]({avatar})]({avatar})")
            if meta:
                lines.append(f"{indent}  {meta}")
            lines.append(f"{indent}  {_markdown_text(comment['text'])}")
            lines.append(f"{indent}  赞 {comment['like_counts']}")
            for media in comment["media"]:
                image = media["browser_url"]
                lines.extend([
                    f"{indent}  [![评论图片]({image})]({image})",
                ])
            lines.append("")
    return "\n".join(lines)


def _get_template_dir() -> Path:
    """获取模板目录路径（兼容 PyInstaller 打包模式）"""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "weibo_book" / "templates"
    return Path(__file__).parent / "templates"


class BookGenerator:
    """微博书生成器"""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 设置 Jinja2 模板环境
        template_dir = _get_template_dir()
        self.env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True,
        )

    def generate_interactive_archive(self, repository, *, render_pdf=None):
        """从归档 repository 生成固定文件名的离线互动微博书。"""
        from .archive.render_snapshot import ArchiveRenderer

        return ArchiveRenderer(repository).render_all(
            self.output_dir, render_pdf=render_pdf
        )

    def _relativize_media_paths(self, posts: list[Post]) -> None:
        """把副本中的本地媒体路径统一转换为相对输出目录路径。"""
        for post in posts:
            for media in post.media:
                for attr in ("local_path", "local_thumb", "video_cover"):
                    value = getattr(media, attr)
                    if not value:
                        continue
                    try:
                        setattr(
                            media,
                            attr,
                            os.path.relpath(value, self.output_dir).replace("\\", "/"),
                        )
                    except ValueError:
                        pass
            if post.link_card and post.link_card.local_image:
                try:
                    post.link_card.local_image = os.path.relpath(
                        post.link_card.local_image, self.output_dir
                    ).replace("\\", "/")
                except ValueError:
                    pass
            if post.retweeted:
                self._relativize_media_paths([post.retweeted])

    def _append_link_card_markdown(self, lines: list[str], post: Post, quote: str = "") -> None:
        card = post.link_card
        if not card:
            return
        label = card.title or card.url or card.original_url
        target = card.url or card.original_url
        if card.local_image:
            image_path = os.path.relpath(card.local_image, self.output_dir)
            lines.append(f"{quote}![引用卡片]({image_path})")
        if label and target:
            lines.append(f"{quote}[🔗 {label}]({target})")
        elif label:
            lines.append(f"{quote}🔗 {label}")
        if card.description:
            lines.append(f"{quote}{card.description}")
        lines.append(quote.rstrip())

    def _post_to_markdown(self, post: Post, index: int) -> str:
        """将单条帖子转为 Markdown 片段"""
        lines = []
        lines.append(f"---")
        lines.append(f"### #{index}  {post.created_at.strftime('%Y-%m-%d %H:%M') if post.created_at else ''}")
        lines.append("")

        # 正文
        if post.text:
            lines.append(post.text)
            lines.append("")

        # 来源
        if post.source:
            lines.append(f"> 来自 {post.source}")
            lines.append("")

        # 位置
        if post.location:
            lines.append(f"> 📍 {post.location}")
            lines.append("")

        # 媒体
        for m in post.media:
            if m.local_path:
                rel_path = os.path.relpath(m.local_path, self.output_dir)
                if m.type == MediaType.IMAGE:
                    lines.append(f"![图片]({rel_path})")
                elif m.type == MediaType.VIDEO:
                    # L6 v1.1.1：视频在 Markdown 渲染器里也不放，只留链接
                    lines.append(f"📹 [视频文件]({rel_path})")
                elif m.type == MediaType.LIVE_PHOTO:
                    live_image = m.local_thumb or m.thumbnail
                    if live_image:
                        thumb_rel = os.path.relpath(live_image, self.output_dir) if os.path.exists(live_image) else live_image
                        lines.append(f"![实况照片]({thumb_rel}) *📱 Live*")
                        lines.append(f"📹 [实况视频]({rel_path})")
                    else:
                        lines.append(f"📹 [实况视频]({rel_path})")
                lines.append("")

        self._append_link_card_markdown(lines, post)

        # 转发
        if post.retweeted:
            lines.append(f"> **转发自 @{post.retweeted.user_name or '原博主'}**")
            r = post.retweeted
            if r.text:
                lines.extend(f"> {line}" for line in r.text.split("\n"))
            for m in r.media:
                source = m.local_thumb or m.video_cover or m.local_path or m.thumbnail or m.url
                if source:
                    rel = os.path.relpath(source, self.output_dir) if os.path.exists(source) else source
                    lines.append(f"> ![原微博媒体]({rel})")
            self._append_link_card_markdown(lines, r, quote="> ")
            lines.append("")

        # 互动数据
        stats = []
        if post.reposts_count:
            stats.append(f"🔄 {post.reposts_count}")
        if post.comments_count:
            stats.append(f"💬 {post.comments_count}")
        if post.likes_count:
            stats.append(f"❤️ {post.likes_count}")
        if stats:
            lines.append(" | ".join(stats))
            lines.append("")

        # 评论
        if post.comments:
            lines.append("**评论：**")
            for c in post.comments:
                tag = " [博主]" if c.is_blogger else ""
                lines.append(f"- {c.user_name}{tag}: {c.text}")
            lines.append("")

        return "\n".join(lines)

    def generate_markdown(
        self,
        posts: list[Post],
        user: UserInfo,
    ) -> str:
        """生成 Markdown 文件

        Returns:
            str: Markdown 文件路径
        """
        filename = f"微博书_{user.screen_name}_{datetime.now():%Y%m%d_%H%M%S}.md"
        filepath = self.output_dir / filename

        lines = []
        lines.append(f"# {user.screen_name} 的微博书")
        lines.append("")
        lines.append(f"> 共 {len(posts)} 条微博 · 生成于 {datetime.now():%Y-%m-%d %H:%M}")
        lines.append("")

        if user.description:
            lines.append(f"> {user.description}")
            lines.append("")

        lines.append(f"> 📍 {user.location}" if user.location else "")
        lines.append(f"> 👤 {user.followers_count} 粉丝 · {user.following_count} 关注")
        lines.append("")
        lines.append("---")
        lines.append("")

        for i, post in enumerate(posts, 1):
            lines.append(self._post_to_markdown(post, i))
            lines.append("")

        content = "\n".join(lines)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("📝 Markdown 已生成: %s", filepath)
        return str(filepath)

    def generate_html(
        self,
        posts: list[Post],
        user: UserInfo,
    ) -> str:
        """生成完整的 HTML 页面

        v1.1.1 修复 L7：不再修改入参 posts 的 media.local_path，
        改为深拷贝副本，避免 generate_pdf() 二次调用时路径叠加（../../../）。
        """
        import copy
        posts_copy = copy.deepcopy(posts)
        self._relativize_media_paths(posts_copy)

        template = self.env.get_template("book.html")

        html = template.render(
            posts=posts_copy,
            user=user,
            generate_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

        filepath = self.output_dir / f"微博书_{user.screen_name}_{datetime.now():%Y%m%d_%H%M%S}.html"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info("📄 HTML 已生成: %s", filepath)
        return str(filepath)

    def _to_jsonable(self, value):
        """递归把 dataclass / datetime / 路径转 JSON 友好对象。"""
        if is_dataclass(value):
            return {k: self._to_jsonable(v) for k, v in asdict(value).items()}
        if isinstance(value, list):
            return [self._to_jsonable(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        return value

    def generate_json(
        self,
        posts: list[Post],
        user: UserInfo,
    ) -> str:
        """导出结构化 JSON：用户 + 全部微博 + 生成元信息。"""
        import copy
        posts_copy = copy.deepcopy(posts)
        self._relativize_media_paths(posts_copy)

        payload = {
            "schema": "weishushu.weibobook/1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "user": self._to_jsonable(user),
            "posts_count": len(posts_copy),
            "posts": self._to_jsonable(posts_copy),
        }

        filename = f"微博书_{user.screen_name}_{datetime.now():%Y%m%d_%H%M%S}.json"
        filepath = self.output_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("🧾 JSON 已生成: %s", filepath)
        return str(filepath)

    def generate_csv(
        self,
        posts: list[Post],
        user: UserInfo,
    ) -> str:
        """导出每条微博一行的 CSV（评论 / 媒体只计数，不展开）。"""
        import copy
        posts_copy = copy.deepcopy(posts)
        for post in posts_copy:
            for media in post.media:
                if media.local_path:
                    try:
                        media.local_path = os.path.relpath(
                            media.local_path, self.output_dir
                        ).replace("\\", "/")
                    except ValueError:
                        pass

        fieldnames = [
            "bid",
            "created_at",
            "is_original",
            "user_name",
            "text",
            "reposts_count",
            "comments_count",
            "likes_count",
            "media_count",
            "source",
            "location",
        ]
        filename = f"微博书_{user.screen_name}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        filepath = self.output_dir / filename
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for post in posts_copy:
                writer.writerow({
                    "bid": post.bid,
                    "created_at": post.created_at.isoformat() if post.created_at else "",
                    "is_original": post.is_original,
                    "user_name": post.user_name,
                    "text": post.text or "",
                    "reposts_count": post.reposts_count,
                    "comments_count": post.comments_count,
                    "likes_count": post.likes_count,
                    "media_count": len(post.media) if isinstance(post.media, list) else 0,
                    "source": post.source or "",
                    "location": post.location or "",
                })
        logger.info("🧾 CSV 已生成: %s", filepath)
        return str(filepath)

    def generate_pdf(
        self,
        posts: list[Post],
        user: UserInfo,
    ) -> str:
        """通过 Playwright 将 HTML 转为 PDF

        v1.1.1 修复 L5：内部启动子线程跑 Playwright，主线程每秒心跳打印进度，
        CLI 模式下用户看到「📄 PDF 渲染中 3s/30s」不会以为死机。
        FastAPI 模式（BackgroundTasks）下本函数在子线程跑，**不**额外起线程（避免线程爆炸）。

        Returns:
            str: PDF 文件路径
        """
        # 先确保 HTML 已生成
        html_path = self.generate_html(posts, user)

        pdf_path = html_path.replace(".html", ".pdf")
        html_url = f"file://{Path(html_path).resolve().as_posix()}"

        from playwright.sync_api import sync_playwright

        logger.info("📄 正在生成 PDF（可能 10-30s）...")

        # L5：检测是否已经在子线程里（BackgroundTasks）—— 在的话直接跑不套线程
        import threading
        already_in_thread = (
            threading.current_thread() is not threading.main_thread()
        )

        def _do_pdf():
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(html_url, wait_until="networkidle")
                page.wait_for_function("window.__WEISHUSHU_PRINT_READY__ === true")
                # 等待所有图片加载
                page.wait_for_timeout(2000)
                page.pdf(
                    path=pdf_path,
                    format="A4",
                    print_background=True,
                    margin={"top": "2cm", "bottom": "2cm", "left": "2.5cm", "right": "2.5cm"},
                )
                browser.close()

        if already_in_thread:
            # 已经在子线程（BackgroundTasks）→ 直接跑
            _do_pdf()
        else:
            # CLI 模式 → 起子线程 + 主线程心跳
            import time as _time
            done_event = threading.Event()
            def _runner():
                try:
                    _do_pdf()
                finally:
                    done_event.set()
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            start = _time.time()
            while not done_event.is_set():
                elapsed = int(_time.time() - start)
                logger.info(f"  ⏳ PDF 渲染中 {elapsed}s")
                done_event.wait(timeout=5.0)
            t.join(timeout=2.0)

        logger.info("📕 PDF 已生成: %s", pdf_path)
        return pdf_path
