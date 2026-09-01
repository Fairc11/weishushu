"""微博书 - 数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MediaType(str, Enum):
    """媒体类型"""
    IMAGE = "image"
    LIVE_PHOTO = "live_photo"  # 实况照片（图片+短视频）
    VIDEO = "video"


class CommentType(str, Enum):
    """评论提取类型"""
    HOT = "hot"          # 热评（按点赞排序）
    BLOGGER = "blogger"  # 仅博主的评论
    ALL = "all"          # 全部评论


class ImageQuality(str, Enum):
    """图片清晰度"""
    THUMB = "thumb180"   # 缩略图（最小）
    MEDIUM = "mw690"     # 中等
    LARGE = "mw1024"     # 大图
    ORIGINAL = "large"   # 原图（默认）
    HQ = "original"      # 超清原图


class ExtractType(str, Enum):
    """提取类型"""
    POSTS = "posts"          # 用户微博
    FAVORITES = "favorites"  # 用户收藏


@dataclass
class PostMedia:
    """帖子中的一条媒体"""
    type: MediaType
    url: str                      # 原始 URL（图片原图 / 视频地址）
    thumbnail: Optional[str] = None  # 缩略图/视频封面 URL
    local_path: Optional[str] = None # 下载后的本地路径
    local_thumb: Optional[str] = None # 下载后的缩略图路径
    width: int = 0
    height: int = 0
    duration: Optional[int] = None     # 视频/实况时长（秒）
    video_cover: Optional[str] = None  # 视频封面本地路径


@dataclass
class LinkCard:
    """微博 `page_info` 引用卡片。"""
    type: str
    title: str
    description: str
    image_url: str
    url: str
    original_url: str = ""
    local_image: Optional[str] = None


@dataclass
class Comment:
    """评论"""
    id: str
    text: str
    user_name: str
    user_id: str
    user_avatar: str
    created_at: str
    like_counts: int
    is_blogger: bool = False  # 是否是博主本人
    reply_to: Optional[str] = None  # 回复谁的评论
    source: str = ""
    image_url: str = ""
    local_image: Optional[str] = None
    parent_id: Optional[str] = None
    reply_to_name: str = ""
    replies: list["Comment"] = field(default_factory=list)


@dataclass
class UserInfo:
    """用户信息"""
    uid: str
    screen_name: str
    avatar_url: str
    description: str = ""
    followers_count: int = 0
    following_count: int = 0
    posts_count: int = 0
    verified: bool = False
    verified_reason: str = ""
    location: str = ""
    gender: str = ""
    cover_image_url: str = ""


@dataclass
class Post:
    """微博帖子"""
    bid: str                   # 微博 ID
    uid: str                   # 作者 UID
    user_name: str             # 作者昵称
    user_avatar: str           # 作者头像 URL
    text: str                  # 正文（已展开全文）
    created_at: Optional[datetime] = None  # 发布时间
    source: str = ""           # 发布来源（如 iPhone 客户端）
    media: list[PostMedia] = field(default_factory=list)  # 媒体列表
    reposts_count: int = 0
    comments_count: int = 0
    likes_count: int = 0
    is_original: bool = True
    retweeted: Optional[Post] = None  # 转发自
    comments: list[Comment] = field(default_factory=list)  # 评论
    location: str = ""
    raw_bid: str = ""          # 原始 BID（用于链接）
    # F3 v1.1.2：作者元信息（用于仿微博 APP 蓝 V / 性别图标）
    verified: bool = False
    gender: str = ""           # "m" | "f" | ""
    link_card: Optional[LinkCard] = None
    ip_location: str = ""
    is_pinned: bool = False
    pin_order: Optional[int] = None
    visibility: str = "visible"
    last_successful_check_at: Optional[datetime] = None


@dataclass
class BookConfig:
    """微博书生成配置"""
    url: str
    max_posts: int = 0           # 0=全部
    output_dir: str = "./output"
    formats: list[str] = field(default_factory=lambda: ["md", "pdf"])

    # 提取类型
    extract_type: ExtractType = ExtractType.POSTS  # posts / favorites

    # 时间筛选
    start_date: Optional[str] = None  # 起始日期 YYYY-MM-DD
    end_date: Optional[str] = None    # 结束日期 YYYY-MM-DD

    # 原创/转载筛选
    only_original: bool = False  # True=仅原创 False=全部

    # 评论配置
    comments: bool = False
    comments_count: int = 5
    comments_type: CommentType = CommentType.HOT

    # 媒体配置
    download_media: bool = True
    image_quality: ImageQuality = ImageQuality.ORIGINAL  # 图片清晰度

    # 登录配置
    login: bool = False
    cookie: Optional[str] = None
    cookie_file: Optional[str] = None
