from __future__ import annotations

from fastapi import FastAPI

from apps.api.routes.admin import router as admin_router
from apps.api.routes.miniapp.users import router as miniapp_users_router
from apps.api.routes.webhooks.nowpayments import router as nowpayments_webhook_router
from apps.api.routes.webhooks.tetrapay import router as tetrapay_webhook_router
from apps.api.routes.dl import router as dl_router


app = FastAPI(title="telegramsellbot-api", version="0.1.0")
app.include_router(miniapp_users_router, prefix="/api/miniapp", tags=["miniapp"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(nowpayments_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(tetrapay_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(dl_router, prefix="/api", tags=["dl"])
