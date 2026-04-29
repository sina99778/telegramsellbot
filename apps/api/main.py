from __future__ import annotations

import logging
import pathlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import text

from core.database import AsyncSessionFactory
from apps.api.routes.admin import router as admin_router
from apps.api.routes.miniapp.users import router as miniapp_users_router
from apps.api.routes.webhooks.nowpayments import router as nowpayments_webhook_router
from apps.api.routes.webhooks.tetrapay import router as tetrapay_webhook_router
from apps.api.routes.dl import router as dl_router
from apps.api.routes.sub import router as sub_router

logger = logging.getLogger(__name__)

MINIAPP_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "miniapp"

app = FastAPI(title="telegramsellbot-api", version="0.1.0")
app.include_router(miniapp_users_router, prefix="/api/miniapp", tags=["miniapp"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(nowpayments_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(tetrapay_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
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
