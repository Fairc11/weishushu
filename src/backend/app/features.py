"""当前版本的中央功能开关。"""

from __future__ import annotations

from typing import Final, NoReturn

from fastapi import HTTPException

SELF_ARCHIVE_ENABLED: Final = True
PROFILE_ARCHIVE_ENABLED: Final = False
EMBEDDED_WEIBO_BROWSER_ENABLED: Final = False
CHROME_IMPORT_ENABLED: Final = False

FUTURE_FEATURE_MESSAGE: Final = "该功能正在开发中。"


def raise_future_feature() -> NoReturn:
    raise HTTPException(status_code=501, detail=FUTURE_FEATURE_MESSAGE)
