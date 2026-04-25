from __future__ import annotations

import pathlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from apps.api.routes.admin import router as admin_router
from apps.api.routes.miniapp.users import router as miniapp_users_router
from apps.api.routes.webhooks.nowpayments import router as nowpayments_webhook_router
from apps.api.routes.webhooks.tetrapay import router as tetrapay_webhook_router
from apps.api.routes.dl import router as dl_router


MINIAPP_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "miniapp"

app = FastAPI(title="telegramsellbot-api", version="0.1.0")
app.include_router(miniapp_users_router, prefix="/api/miniapp", tags=["miniapp"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(nowpayments_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(tetrapay_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(dl_router, prefix="/api", tags=["dl"])

# ─── Serve Mini App static files ─────────────────────────────────────────────
if MINIAPP_DIR.exists():
    app.mount("/miniapp/css", StaticFiles(directory=str(MINIAPP_DIR / "css")), name="miniapp-css")
    app.mount("/miniapp/js", StaticFiles(directory=str(MINIAPP_DIR / "js")), name="miniapp-js")
    app.mount("/miniapp/assets", StaticFiles(directory=str(MINIAPP_DIR / "assets")), name="miniapp-assets")

    @app.get("/miniapp")
    @app.get("/miniapp/")
    async def serve_miniapp():
        return FileResponse(str(MINIAPP_DIR / "index.html"), media_type="text/html")
