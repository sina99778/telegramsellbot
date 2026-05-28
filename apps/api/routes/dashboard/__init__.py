"""
Dashboard API router — /api/dashboard/*

Mounted from apps/api/main.py. Sub-modules add their own sub-routers:

    auth.py      — login / logout / me
    overview.py  — KPI cards + chart series (added in a follow-up)
    users.py     — user list, detail, actions
    servers.py   — X-UI panel CRUD + health
    txns.py      — wallet transactions + orders

All sub-routers (except auth) depend on `require_dashboard_admin` so a
forgotten endpoint is never publicly reachable.
"""
from fastapi import APIRouter

from apps.api.routes.dashboard.auth import router as auth_router
from apps.api.routes.dashboard.overview import router as overview_router
from apps.api.routes.dashboard.users import router as users_router


router = APIRouter()
router.include_router(auth_router, prefix="/auth", tags=["dashboard-auth"])
router.include_router(overview_router, prefix="/overview", tags=["dashboard-overview"])
router.include_router(users_router, prefix="/users", tags=["dashboard-users"])


__all__ = ["router"]
