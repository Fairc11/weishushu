"""FastAPI 入口。阶段 1 只挂 /healthz + / 模板渲染。阶段 2 再加 8 个 router。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.app.config import settings
from backend.app.deps import lifespan_context
from backend.app.services import log_handler
from backend.app.version import VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log_handler.install(logging.INFO)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=VERSION,
        lifespan=lifespan_context,
        docs_url=None,  # 桌面应用不暴露 /docs
        redoc_url=None,
    )

    # 静态资源
    static_dir: Path = settings.static_dir
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 模板
    templates_dir: Path = settings.templates_dir
    templates_dir.mkdir(parents=True, exist_ok=True)
    templates = Jinja2Templates(directory=str(templates_dir))

    # ====== /ws 必须最先挂（避免被 catch-all 拦截）======
    from backend.app.routers.router_ws import router as ws_router
    app.include_router(ws_router)

    # ====== 业务 router（按功能顺序）======
    from backend.app.routers.router_profile import router as profile_router
    from backend.app.routers.router_scraper import router as scraper_router
    from backend.app.routers.router_login import router as login_router
    from backend.app.routers.router_download import router as download_router
    from backend.app.routers.router_tasks import router as tasks_router
    from backend.app.routers.router_logs import router as logs_router
    from backend.app.routers.router_assets import router as assets_router
    from backend.app.routers.router_favorites import router as favorites_router  # v1.1.3 U1
    from backend.app.routers.router_backup import router as backup_router  # v1.1.5
    from backend.app.routers.router_history import router as history_router  # v1.2.0 M3-10
    from backend.app.routers.router_first_run import router as first_run_router  # v1.2.0 V120-1
    from backend.app.routers.router_browser import router as browser_router  # v1.2.0 V120-3
    from backend.app.routers.router_search import router as search_router  # v2.0.1 备份他人微博

    for r in (profile_router, scraper_router, login_router, download_router,
              tasks_router, logs_router, assets_router, favorites_router, backup_router,
              history_router, first_run_router, browser_router, search_router):
        app.include_router(r)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": VERSION})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_name": settings.app_name,
                "version": VERSION,
                "platform": sys.platform,
            },
        )

    logger.info(
        "FastAPI app created: %s v%s (frozen=%s, routers=%d)",
        settings.app_name, VERSION, getattr(sys, "frozen", False),
        len(app.routes),
    )
    return app


app = create_app()
