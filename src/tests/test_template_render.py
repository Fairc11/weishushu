"""仿微博 APP 模板渲染单测（v1.1.2 F3）。"""

import sys
import unittest
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jinja2 import Environment, FileSystemLoader
from weibo_book.models import Post, PostMedia, MediaType, UserInfo
from weibo_book.generator import BookGenerator


def _make_env():
    tpl_dir = Path(__file__).resolve().parents[1] / "weibo_book" / "templates"
    return Environment(loader=FileSystemLoader(str(tpl_dir)))


def _make_user():
    return UserInfo(
        uid="123",
        screen_name="测试博主",
        avatar_url="https://wx1.sinaimg.cn/avatar.jpg",
        description="演员",
        followers_count=57000,
        following_count=120,
        posts_count=1234,
        verified=True,
        verified_reason="演员",
        gender="f",
        cover_image_url="",
        location="",
    )


def _make_post(text="麻花辫野生选手 #非同小可", media_count=0, verified=True, gender="f"):
    media = []
    for i in range(media_count):
        media.append(PostMedia(type=MediaType.IMAGE, url=f"https://wx1.sinaimg.cn/large/p{i}.jpg",
                               thumbnail=f"https://wx1.sinaimg.cn/thumb180/p{i}.jpg"))
    return Post(
        bid="abc123",
        uid="123",
        user_name="测试博主",
        user_avatar="https://wx1.sinaimg.cn/avatar.jpg",
        text=text,
        created_at=datetime(2026, 6, 2, 18, 52),
        source="iPhone 15 Pro Max",
        reposts_count=10,
        comments_count=20,
        likes_count=30,
        media=media,
        verified=verified,
        gender=gender,
    )


class TemplateRenderTests(unittest.TestCase):
    """v1.1.2 F3：仿微博 APP 模板 + 9 张 3×3 渲染"""

    def setUp(self):
        self.env = _make_env()

    def test_1_image_uses_1fr(self):
        """1 张图 → 单图渲染（m.local_path 优先，否则 thumbnail 兜底）"""
        tpl = self.env.get_template("card.html")
        post = _make_post(media_count=1)
        html = tpl.render(post=post)
        self.assertIn("media-grid", html)
        # thumb180/p0.jpg 是 thumbnail（local_path 为空时兜底）
        self.assertIn('src="https://wx1.sinaimg.cn/thumb180/p0.jpg"', html)

    def test_9_images_renders_9_items(self):
        """9 张图 → 9 个 image-item 节点（3×3 由 CSS 决定）"""
        tpl = self.env.get_template("card.html")
        post = _make_post(media_count=9)
        html = tpl.render(post=post)
        self.assertEqual(html.count('class="media-item image-item"'), 9)

    def test_18_images_render_in_original_order(self):
        tpl = self.env.get_template("card.html")
        post = _make_post(media_count=18)

        html = tpl.render(post=post)

        self.assertEqual(html.count('class="media-item image-item"'), 18)
        positions = [html.index(f"thumb180/p{i}.jpg") for i in range(18)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('data-media-count="18"', html)

    def test_10_to_18_layout_uses_three_columns_without_cropping(self):
        css = (Path(__file__).resolve().parents[1] / "weibo_book" / "templates" / "book.html").read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
        self.assertIn("object-fit: contain;", css)

    def test_print_media_stays_inside_square_and_footer_does_not_force_blank_page(self):
        css = (Path(__file__).resolve().parents[1] / "weibo_book" / "templates" / "book.html").read_text(encoding="utf-8")

        self.assertNotIn(".media-item img {\n    width: 100%;\n    height: auto;", css)
        self.assertIn("page-break-inside: avoid !important;", css)
        self.assertIn("break-inside: avoid-page !important;", css)
        self.assertIn(".footer-page {\n      display: none;", css)
        self.assertIn("text-overflow: ellipsis;", css)

    def test_blue_v_svg_for_verified_user(self):
        """verified=True → SVG 蓝 V 节点存在"""
        tpl = self.env.get_template("card.html")
        post = _make_post(verified=True)
        html = tpl.render(post=post)
        self.assertIn('class="verified-icon"', html)
        self.assertIn('viewBox="0 0 16 16"', html)

    def test_no_blue_v_for_unverified(self):
        """verified=False → 没有蓝 V 节点"""
        tpl = self.env.get_template("card.html")
        post = _make_post(verified=False)
        html = tpl.render(post=post)
        self.assertNotIn('class="verified-icon"', html)

    def test_gender_female_icon(self):
        """gender='f' → ♀ 节点"""
        tpl = self.env.get_template("card.html")
        post = _make_post(gender="f")
        html = tpl.render(post=post)
        self.assertIn('class="gender-female"', html)
        self.assertIn("♀", html)

    def test_gender_male_icon(self):
        """gender='m' → ♂ 节点"""
        tpl = self.env.get_template("card.html")
        post = _make_post(gender="m")
        html = tpl.render(post=post)
        self.assertIn('class="gender-male"', html)
        self.assertIn("♂", html)

    def test_no_gender_icon(self):
        """gender='' → 没有性别节点"""
        tpl = self.env.get_template("card.html")
        post = _make_post(gender="")
        html = tpl.render(post=post)
        self.assertNotIn('class="gender-female"', html)
        self.assertNotIn('class="gender-male"', html)

    def test_actions_bar_uses_svg_not_emoji(self):
        """互动栏用 inline SVG 替换 emoji"""
        tpl = self.env.get_template("card.html")
        post = _make_post()
        html = tpl.render(post=post)
        self.assertIn('class="actions-bar"', html)
        self.assertIn("<svg", html)
        # 旧的 emoji 互动数字 应当不再以 🔄💬❤️ 起头
        self.assertNotIn("🔄", html)
        self.assertNotIn("💬", html)
        self.assertNotIn("❤️", html)

    def test_video_media_renders_play_icon(self):
        """视频媒体 → ▶ play-icon"""
        from weibo_book.models import PostMedia
        post = _make_post(media_count=0)
        post.media = [PostMedia(type=MediaType.VIDEO, url="https://video.weibo.com/v.mp4",
                                thumbnail="https://wx1.sinaimg.cn/cover.jpg", duration=15)]
        tpl = self.env.get_template("card.html")
        html = tpl.render(post=post)
        self.assertIn("video-item", html)
        self.assertIn("play-icon", html)
        self.assertIn("0:15", html)  # duration 格式 0:15

    def test_live_photo_renders_live_badge(self):
        """实况照片 → LIVE 徽章"""
        from weibo_book.models import PostMedia
        post = _make_post(media_count=0)
        post.media = [PostMedia(type=MediaType.LIVE_PHOTO, url="https://video.weibo.com/live.mp4",
                                thumbnail="https://wx1.sinaimg.cn/live.jpg", duration=5)]
        tpl = self.env.get_template("card.html")
        html = tpl.render(post=post)
        self.assertIn("live-photo-item", html)
        self.assertIn("live-badge", html)
        self.assertIn("LIVE", html)

    def test_generated_html_uses_relative_local_live_photo_paths(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            output = Path(td)
            media_dir = output / "media"
            media_dir.mkdir()
            image = media_dir / "abc_live_01.jpg"
            video = media_dir / "abc_live_01.mov"
            image.write_bytes(b"image")
            video.write_bytes(b"video")
            post = _make_post(media_count=0)
            post.media = [PostMedia(
                type=MediaType.LIVE_PHOTO,
                url="https://media.example/live.mov",
                thumbnail="https://media.example/live.jpg",
                local_path=str(video),
                local_thumb=str(image),
            )]

            html_path = BookGenerator(output).generate_html([post], _make_user())
            html = Path(html_path).read_text(encoding="utf-8")

            self.assertIn('src="media/abc_live_01.jpg"', html)
            self.assertIn('href="media/abc_live_01.mov"', html)
            self.assertNotIn(str(output), html)

    def test_book_html_renders_multiple_cards(self):
        """book.html 渲染多张卡片（顶部封面 + N 卡）"""
        posts = [_make_post(media_count=9, verified=True) for _ in range(3)]
        user = _make_user()
        tpl = self.env.get_template("book.html")
        html = tpl.render(posts=posts, user=user)
        self.assertIn("测试博主", html)
        # 3 张卡片
        self.assertEqual(html.count('class="weibo-card"'), 3)
        # 蓝 V 在每张卡
        self.assertEqual(html.count('class="verified-icon"'), 3)
        # 9 张图 × 3 卡片 = 27 个 image-item
        self.assertEqual(html.count('class="media-item image-item"'), 27)

    def test_archive_mode_keeps_old_template_and_adds_only_local_controls(self):
        """互动归档与旧 PDF 模板共用文件，但归档分支不加载远程资源。"""
        from types import SimpleNamespace

        snapshot = SimpleNamespace(
            user={"screen_name": "固定名字"}, posts=tuple()
        )
        html = self.env.get_template("book.html").render(
            archive_mode=True, snapshot=snapshot
        )

        self.assertIn('data-view="feed"', html)
        self.assertIn('data-view="detail"', html)
        self.assertIn('<script src="data/archive-data.js"></script>', html)
        self.assertNotIn("https://", html)
        self.assertNotIn("fetch(", html)

    def test_archive_print_contract_keeps_deep_links_without_technical_appendices(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "weibo_book"
            / "templates"
            / "book.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("image-appendix-", source)
        self.assertNotIn(".pdf-appendices", source)
        self.assertNotIn("本地文件：", source)
        self.assertIn("&media=", source)
        self.assertIn("function parseArchiveHash", source)

    def test_archive_print_template_defines_a4_page_and_book_chrome(self):
        """归档打印分支具备 A4 @page 规则与书版式封面/目录/章节锚点。"""
        source = (
            Path(__file__).resolve().parents[1]
            / "weibo_book"
            / "templates"
            / "book.html"
        ).read_text(encoding="utf-8")
        archive_branch = source.split("{% if archive_mode %}", 1)[1].split(
            "{% else %}", 1
        )[0]

        self.assertIn("@page", archive_branch)
        self.assertIn("A4", archive_branch)
        self.assertIn("pdf-cover", archive_branch)
        self.assertIn("pdf-toc", archive_branch)
        self.assertIn("chapter-", archive_branch)


if __name__ == "__main__":
    unittest.main()
