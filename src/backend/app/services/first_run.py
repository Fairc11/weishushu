"""v2.0.1 首启风险须知标记文件服务。

读/写首启标记文件：
- frozen 模式：`settings.state_dir / first_run_v2.0.1.json`
- dev 模式：项目根 `.run/state/first_run_v2.0.1.json`

标记文件存在 = 用户已接受 v2.0.1 风险须知（10 不做清单）。
前端启动时调 `/api/first-run/check` 判断是否弹模态。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from backend.app.config import settings

logger = logging.getLogger(__name__)

MARKER_FILENAME = "first_run_v2.0.1.json"
SCHEMA_VERSION = 1
ACCEPTED_VERSION = "2.0.1"


def _marker_path() -> Path:
    """首启标记统一放到 settings.state_dir。"""
    return settings.state_dir / MARKER_FILENAME


def is_accepted() -> bool:
    """标记文件存在 → 已接受。"""
    return _marker_path().exists()


def accept() -> Path:
    """写标记文件（覆盖），返文件路径。"""
    p = _marker_path()
    p.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "version": ACCEPTED_VERSION,
                "accepted_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("v2.0.1 风险须知已接受：%s", p)
    return p
