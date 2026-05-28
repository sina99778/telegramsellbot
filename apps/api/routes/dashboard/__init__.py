"""
Dashboard API router — /api/dashboard/*

Mounted from apps/api/main.py. Sub-modules add their own sub-routers:

    auth.py         — login / logout / me
    overview.py     — KPI cards + chart series
    users.py        — user list, detail, actions
    servers.py      — X-UI panel CRUD + health
    transactions.py — wallet transactions + orders
    settings.py     — global app settings (currency, backup, premium emoji…)
    plans.py        — subscription plans CRUD
    discounts.py    — discount codes CRUD
    broadcast.py    — broadcast composer (queues into BroadcastJob)

All sub-routers (except auth) depend on `require_dashboard_admin` so a
forgotten endpoint is never publicly reachable.
"""
from fastapi import APIRouter

from apps.api.routes.dashboard.auth import router as auth_router
from apps.api.routes.dashboard.broadcast import router as broadcast_router
from apps.api.routes.dashboard.discounts import router as discounts_router
from apps.api.routes.dashboard.overview import router as overview_router
from apps.api.routes.dashboard.plans import router as plans_router
from apps.api.routes.dashboard.servers import router as servers_router
from apps.api.routes.dashboard.settings import router as settings_router
from apps.api.routes.dashboard.transactions import router as transactions_router
from apps.api.routes.dashboard.users import router as users_router


router = APIRouter()
router.include_router(auth_router, prefix="/auth", tags=["dashboard-auth"])
router.include_router(broadcast_router, prefix="/broadcast", tags=["dashboard-broadcast"])
router.include_router(discounts_router, prefix="/discounts", tags=["dashboard-discounts"])
router.include_router(overview_router, prefix="/overview", tags=["dashboard-overview"])
router.include_router(plans_router, prefix="/plans", tags=["dashboard-plans"])
router.include_router(servers_router, prefix="/servers", tags=["dashboard-servers"])
router.include_router(settings_router, prefix="/settings", tags=["dashboard-settings"])
router.include_router(transactions_router, prefix="/transactions", tags=["dashboard-transactions"])
router.include_router(users_router, prefix="/users", tags=["dashboard-users"])


__all__ = ["router"]
