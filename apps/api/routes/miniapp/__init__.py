"""Mini App routes package."""
from apps.api.routes.miniapp.qr import router as qr_router
from apps.api.routes.miniapp.users import router as users_router

# apps/api/main.py includes the sibling routers (users, brand, payments,
# service_actions) explicitly under the /api/miniapp prefix. Attaching the
# local QR-rendering routes to the users router here — this package
# __init__ runs before main.py's submodule imports resolve — exposes
# GET /api/miniapp/qr without main.py having to know about the new module.
users_router.include_router(qr_router)
