"""Pydantic 模型。阶段 1 集中放一个文件，阶段 2 再拆。

v1.2.0 P0 (B-09)：补齐 max_posts / comments_count / formats / extract_type / dates 校验。
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ====== 通用：format / extract_type / image_quality 枚举 ======
FormatLiteral = Literal["md", "pdf", "html", "json", "csv"]
ExtractTypeLiteral = Literal["posts", "favorites"]
ImageQualityLiteral = Literal["thumb180", "mw690", "mw1024", "large", "original"]


def _validate_date_range(start_date: Optional[str], end_date: Optional[str]) -> None:
    """校验可选 ISO 日期边界，保持普通提取与一键备份语义一致。"""
    parsed_start = None
    parsed_end = None
    if start_date:
        try:
            parsed_start = date.fromisoformat(start_date)
            if parsed_start.isoformat() != start_date:
                raise ValueError("日期格式不匹配")
        except ValueError as exc:
            raise ValueError("start_date 必须是 YYYY-MM-DD") from exc
    if end_date:
        try:
            parsed_end = date.fromisoformat(end_date)
            if parsed_end.isoformat() != end_date:
                raise ValueError("日期格式不匹配")
        except ValueError as exc:
            raise ValueError("end_date 必须是 YYYY-MM-DD") from exc
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise ValueError("开始日期不能晚于结束日期")


# ====== Profile ======
class ResolveURLRequest(BaseModel):
    url: str = Field(..., min_length=1, description="微博主页 URL")


class ResolveURLResponse(BaseModel):
    uid: str
    user: dict  # UserInfo → dict（前端不关心 dataclass）


# ====== Scraper ======
class PreviewRequest(BaseModel):
    url: str
    count: int = Field(20, ge=1, le=50)


class StartRequest(BaseModel):
    url: str
    max_posts: int = Field(0, ge=0, le=100000, description="0=不限制")
    formats: list[FormatLiteral] = Field(default_factory=lambda: ["md", "pdf"])
    comments: bool = False
    comments_count: int = Field(5, ge=0, le=100)
    comments_type: str = "hot"
    download_media: bool = True
    image_quality: ImageQualityLiteral = "large"
    login: bool = False  # 显式触发扫码（无缓存时）
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    only_original: bool = False
    extract_type: ExtractTypeLiteral = "posts"
    post_ids: Optional[list[str]] = None

    @model_validator(mode="after")
    def _check_dates(self) -> "StartRequest":
        """已填日期必须为 YYYY-MM-DD，且开始日期不得晚于结束日期。"""
        _validate_date_range(self.start_date, self.end_date)
        return self


class StartResponse(BaseModel):
    task_id: str


# ====== Login ======
class LoginStatus(BaseModel):
    logged_in: bool
    cookie_source: Optional[str] = None  # "file" | "none" | "expired" | "error"


# ====== Tasks ======
class TaskSnapshot(BaseModel):
    id: str
    state: str               # pending | running | done | error | cancelled
    progress_pct: float
    progress_msg: str
    progress_event: Optional[dict] = None
    started_at: float
    finished_at: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[str] = None


# ====== Logs ======
class LogEntry(BaseModel):
    ts: float
    level: str
    msg: str


# ====== Assets ======
# 防盗链代理走 query 参数，不用 body


# ====== v1.1.5 一键备份 ======
class WhoamiResponse(BaseModel):
    """GET /api/login/whoami 返回"""
    uid: str
    screen_name: str
    avatar_url: str = ""
    followers_count: int = 0
    following_count: int = 0
    posts_count: int = 0
    verified: bool = False
    description: str = ""


class BackupIndex(BaseModel):
    """<output_dir>/.weishushu_index.json"""
    schema_version: int = 1
    uid: str
    screen_name: str = ""
    last_backup_at: float = 0.0
    last_backup_count: int = 0      # 本次新增数
    total_backed_up: int = 0        # 累计已备
    bids: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)  # bid → 文件名
    format_hashes: dict[str, str] = Field(default_factory=dict)  # v1.2 预留


class BackupRequest(BaseModel):
    """POST /api/backup/start"""
    output_dir: str                                       # 用户选的绝对路径
    max_posts: int = Field(50, ge=0, le=100000, description="默认最近 50 条，0=不限制")
    formats: list[FormatLiteral] = Field(default_factory=lambda: ["md", "pdf"])
    comments: bool = False
    comments_count: int = Field(5, ge=0, le=100)
    comments_type: str = "hot"
    download_media: bool = True
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    post_ids: Optional[list[str]] = None
    refresh_index: bool = True                            # 备份完写索引
    confirm_uid_mismatch: bool = False                    # 目录 uid 不一致时用户显式确认

    @model_validator(mode="after")
    def _check_scope(self) -> "BackupRequest":
        _validate_date_range(self.start_date, self.end_date)
        if self.post_ids is not None and not self.post_ids:
            raise ValueError("请至少选择一条微博")
        return self


class BackupStartResponse(BaseModel):
    task_id: str
    self_uid: str
    self_screen_name: str
    is_incremental: bool
    skipped_bids: int = 0
    to_backup_bids: int = 0


class ArchiveFolderInspection(BaseModel):
    state: Literal[
        "empty",
        "archive",
        "ordinary_nonempty",
        "uid_mismatch",
        "damaged",
        "legacy_index",
    ]
    path: str
    uid: str = ""
    screen_name: str = ""
    total_posts: int = 0
    last_successful_sync_at: str = ""
    message: str = ""


class ArchiveInspectRequest(BaseModel):
    """只读检查微博书目录。``target_uid`` 缺省为当前登录账号（本人）。"""

    model_config = ConfigDict(extra="forbid")

    output_dir: str
    target_uid: str | None = None


class PersonalArchiveRequest(BaseModel):
    """启动微博书归档。``target_uid`` 缺省为当前登录账号（本人），
    提供时为备份指定博主的公开微博。"""

    model_config = ConfigDict(extra="forbid")

    output_dir: str
    mode: Literal["create", "incremental", "rebuild"]
    pacing_mode: Literal[
        "standard",
        "low_2_3_hours",
        "low_4_6_hours",
        "low_8_12_hours",
    ]
    keep_awake_when_plugged: bool = Field(..., strict=True)
    target_uid: str | None = None


class PersonalArchiveStartResponse(BaseModel):
    task_id: str
    mode: Literal["create", "incremental", "rebuild"]
    self_uid: str
    self_screen_name: str


class FollowingArchiveRequest(BaseModel):
    """独立更新当前账号现有微博书中的关注资料。"""

    model_config = ConfigDict(extra="forbid")

    output_dir: str
    pacing_mode: Literal[
        "standard",
        "low_2_3_hours",
        "low_4_6_hours",
        "low_8_12_hours",
    ]
    keep_awake_when_plugged: bool = Field(..., strict=True)


class FollowingArchiveStartResponse(BaseModel):
    task_id: str
    mode: Literal["update"] = "update"
    self_uid: str
    self_screen_name: str


class FollowingDurationCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_dir: str
    object_type: Literal["blogger", "supertopic"]
    object_id: str = Field(..., min_length=1)


class FollowingDurationCheckResponse(BaseModel):
    object_type: Literal["blogger", "supertopic"]
    object_id: str
    source: Literal["platform", "local_minimum"]
    platform_followed_at: str
    local_first_seen_at: str
    last_confirmed_at: str
    currently_following: bool


class RecoveryTaskSummary(BaseModel):
    task_id: str
    task_kind: Literal["personal_archive", "following_archive"]
    mode: Literal["create", "incremental", "rebuild", "update"]
    state: Literal["waiting_resume", "error"]
    phase: Literal["sync", "render", "bloggers", "supertopics", "duration"]
    progress_current: int
    progress_total: int | None
    progress_unit: str
    started_at: str
    saved_at: str
    pause_reason: str
    saved_content: str
    error_recoverable: bool
    pacing_mode: Literal[
        "standard",
        "low_2_3_hours",
        "low_4_6_hours",
        "low_8_12_hours",
    ]
    keep_awake_when_plugged: bool
    pacing_state: Literal[
        "standard",
        "estimating",
        "waiting",
        "requesting",
        "power_saving",
        "paused",
    ]
    pacing_request_kind: Literal["profile", "detail", "comments", "media"] | None
    next_wait_seconds: float | None
    target_label: str | None = None


class RecoveryTaskResponse(BaseModel):
    task: RecoveryTaskSummary | None


class PersonalArchiveTaskActionResponse(BaseModel):
    task_id: str
    state: str


class HistoryEntry(BaseModel):
    """v1.1.6 历史记录面板每条"""
    filename: str          # 微博书_xxx_20260602_180000.md
    filepath: str          # 绝对路径
    created_at: float      # mtime
    size_bytes: int
    bids_count: int = 0    # 解析 md 里的 BID 数（粗估）


class HistoryListResponse(BaseModel):
    entries: list[HistoryEntry]
    total: int


class SearchHit(BaseModel):
    """v1.1.6 全局搜索每条命中"""
    filename: str
    filepath: str
    line_no: int
    line_text: str
    context_before: str = ""
    context_after: str = ""


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    query: str
    files_scanned: int
