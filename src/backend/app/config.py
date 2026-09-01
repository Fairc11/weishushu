"""frozen/dev 双路径策略。

- dev 模式（`python run.py`）：资源在项目根 `backend/app/{templates,static}`，输出在 `./output`。
- frozen 模式（PyInstaller `console=False`）：资源在 `sys._MEIPASS/backend/app/...`，输出、
  日志和状态目录由 `backend.app.platform_paths` 按平台决定。
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.platform_paths import ensure_dir, platform_paths


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


class Settings(BaseSettings):
    """单例：frozen/dev 都用它，不用各处写 if。"""

    model_config = SettingsConfigDict(env_prefix="WEISHUSHU_", env_file=".env", extra="ignore")

    app_name: str = "微书薯"
    version: str = "2.0.1"
    host: str = "127.0.0.1"
    port: int = 18080  # 由 desktop_app.find_free_port 实际覆盖

    # 用户可见输出目录：dev 用 ./output，frozen 用平台用户数据目录。
    @property
    def output_dir(self) -> Path:
        return ensure_dir(platform_paths().output_dir())

    # 模板/静态资源根：统一定义为 RuntimeContext.resource_root 下的 backend/app。
    @property
    def backend_root(self) -> Path:
        from backend.app.runtime_context import resolve_runtime_context

        return resolve_runtime_context().resource_root / "backend" / "app"

    @property
    def templates_dir(self) -> Path:
        return self.backend_root / "templates"

    @property
    def static_dir(self) -> Path:
        return self.backend_root / "static"

    # 微博书渲染模板（保留原 weibo_book/templates/，PyInstaller 一起打包）
    @property
    def weibobook_templates_dir(self) -> Path:
        from backend.app.runtime_context import resolve_runtime_context

        return resolve_runtime_context().resource_root / "weibo_book" / "templates"

    # ====== B07 v1.2.0: 统一用户数据目录（全英文子目录，避免 GBK 路径兼容性问题） ======

    @property
    def local_app_data_dir(self) -> Path:
        """frozen → 平台用户数据目录；dev → 项目根 ./.run"""
        return ensure_dir(platform_paths().local_app_data_dir())

    @property
    def log_dir(self) -> Path:
        """统一日志目录：frozen → 平台日志目录；dev → 项目根 ./.run/logs。"""
        return ensure_dir(platform_paths().log_dir())

    @property
    def state_dir(self) -> Path:
        """用户状态目录（备份索引、设置、cookie 缓存等）"""
        return ensure_dir(platform_paths().state_dir())


settings = Settings()
is_frozen = _is_frozen()
