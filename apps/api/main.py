from __future__ import annotations

import logging
import pathlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import text

from core.database import AsyncSessionFactory
from apps.api.routes.admin import router as admin_router
from apps.api.routes.miniapp.brand import router as miniapp_brand_router
from apps.api.routes.miniapp.users import router as miniapp_users_router
from apps.api.routes.webhooks.nowpayments import router as nowpayments_webhook_router
from apps.api.routes.webhooks.tetrapay import router as tetrapay_webhook_router
from apps.api.routes.webhooks.tronado import router as tronado_webhook_router
from apps.api.routes.dl import router as dl_router
from apps.api.routes.sub import router as sub_router
from apps.api.routes.dashboard import router as dashboard_router

logger = logging.getLogger(__name__)

MINIAPP_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "miniapp"
DASHBOARD_DIST_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "dashboard" / "dist"

app = FastAPI(title="telegramsellbot-api", version="0.1.0")
app.include_router(miniapp_users_router, prefix="/api/miniapp", tags=["miniapp"])
app.include_router(miniapp_brand_router, prefix="/api/miniapp", tags=["miniapp-brand"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(nowpayments_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(tetrapay_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(tronado_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(dl_router, prefix="/api", tags=["dl"])
app.include_router(sub_router, prefix="/api", tags=["sub"])


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    async with AsyncSessionFactory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}

# ─── Serve Mini App static files ─────────────────────────────────────────────
logger.info("Looking for miniapp at: %s (exists=%s)", MINIAPP_DIR, MINIAPP_DIR.exists())

if MINIAPP_DIR.exists():
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class NoCacheMiniAppMiddleware(BaseHTTPMiddleware):
        """Prevent Telegram WebApp from caching miniapp CSS/JS/HTML files."""
        async def dispatch(self, request: Request, call_next):
            response: Response = await call_next(request)
            if request.url.path.startswith("/miniapp/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
            return response

    app.add_middleware(NoCacheMiniAppMiddleware)

    # Mount sub-directories for static assets
    for subdir in ("css", "js", "assets"):
        sub_path = MINIAPP_DIR / subdir
        if sub_path.exists():
            app.mount(f"/miniapp/{subdir}", StaticFiles(directory=str(sub_path)), name=f"miniapp-{subdir}")

    @app.get("/miniapp")
    @app.get("/miniapp/")
    async def serve_miniapp():
        return FileResponse(
            str(MINIAPP_DIR / "index.html"),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    logger.info("Mini App mounted at /miniapp/")
else:
    logger.warning("miniapp directory not found at %s — Mini App will not be served.", MINIAPP_DIR)


# ─── Serve Dashboard SPA static files ────────────────────────────────────────
#
# The Vue 3 SPA is built into `dashboard/dist/` during the Docker build
# (multi-stage; see Dockerfile). We serve every nested asset path, plus
# fall back to index.html for unknown paths so Vue Router's history-mode
# routes (e.g. /dashboard/users/123) load correctly on a hard refresh.
logger.info("Looking for dashboard build at: %s (exists=%s)",
            DASHBOARD_DIST_DIR, DASHBOARD_DIST_DIR.exists())

if DASHBOARD_DIST_DIR.exists():
    from starlette.requests import Request as _DashboardReq
    from starlette.responses import FileResponse as _DashboardFile, Response as _DashboardResp

    # Mount /dashboard/assets/ for chunks (Vite emits hashed filenames
    # under /assets/ by default; we keep that prefix to stay
    # build-tool-agnostic).
    _assets = DASHBOARD_DIST_DIR / "assets"
    if _assets.exists():
        app.mount("/dashboard/assets", StaticFiles(directory=str(_assets)), name="dashboard-assets")

    _index = DASHBOARD_DIST_DIR / "index.html"

    @app.get("/dashboard")
    @app.get("/dashboard/")
    @app.get("/dashboard/{path:path}")
    async def _serve_dashboard(path: str = "") -> _DashboardResp:
        # Direct hits for top-level files (favicon, robots.txt, …)
        if path:
            candidate = DASHBOARD_DIST_DIR / path
            if candidate.is_file():
                return _DashboardFile(str(candidate))
        # Vue Router fallback: any unknown sub-path serves the SPA shell.
        return _DashboardFile(
            str(_index),
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    logger.info("Dashboard SPA mounted at /dashboard/")
else:
    logger.warning(
        "dashboard/dist not found at %s — dashboard SPA disabled. "
        "Build with `cd dashboard && npm ci && npm run build`, or rely on "
        "the multi-stage Docker build to do it for you.",
        DASHBOARD_DIST_DIR,
    )
