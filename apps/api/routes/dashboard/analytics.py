"""
Financial intelligence for the dashboard.

    GET /api/dashboard/analytics — LTV / top customers, revenue by plan, churn
                                   and headline money KPIs.

Revenue is summed from Order.amount over the statuses that mean money was
actually realised — including "completed" (the renewal flow's status), which
the operational overview omits, so renewal revenue is counted here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.order import Order
from models.plan import Plan
from models.subscription import Subscription
from models.user import User

router = APIRouter()
AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]

# Order statuses that count as realised revenue (incl. renewals = "completed").
_REVENUE_STATUSES = ("paid", "processing", "provisioned", "completed")
_ACTIVE_SUB_STATUSES = ("active", "pending_activation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _sum_revenue(session: AsyncSession, *, since: datetime | None = None) -> float:
    stmt = select(func.coalesce(func.sum(Order.amount), 0)).where(Order.status.in_(_REVENUE_STATUSES))
    if since is not None:
        stmt = stmt.where(Order.created_at >= since)
    return float(await session.scalar(stmt) or 0)


@router.get("")
async def get_analytics(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    now = _now()
    d30, d7 = now - timedelta(days=30), now - timedelta(days=7)

    total_revenue = await _sum_revenue(session)
    revenue_30d = await _sum_revenue(session, since=d30)
    revenue_7d = await _sum_revenue(session, since=d7)

    orders_count = int(await session.scalar(
        select(func.count(Order.id)).where(Order.status.in_(_REVENUE_STATUSES))
    ) or 0)
    paying_users = int(await session.scalar(
        select(func.count(func.distinct(Order.user_id))).where(Order.status.in_(_REVENUE_STATUSES))
    ) or 0)
    active_subscribers = int(await session.scalar(
        select(func.count(func.distinct(Subscription.user_id)))
        .where(Subscription.status.in_(_ACTIVE_SUB_STATUSES))
    ) or 0)
    new_users_30d = int(await session.scalar(
        select(func.count(User.id)).where(User.created_at >= d30)
    ) or 0)

    arpu = (total_revenue / paying_users) if paying_users else 0.0
    avg_order_value = (total_revenue / orders_count) if orders_count else 0.0
    # Lapsed payers: paid before, but no active service now.
    churned_users = max(paying_users - active_subscribers, 0)
    retention_rate = (active_subscribers / paying_users * 100) if paying_users else 0.0

    # ── Top customers by lifetime value ──
    rows = (await session.execute(
        select(
            User.id, User.first_name, User.username, User.telegram_id,
            func.coalesce(func.sum(Order.amount), 0).label("spent"),
            func.count(Order.id).label("orders"),
        )
        .join(Order, Order.user_id == User.id)
        .where(Order.status.in_(_REVENUE_STATUSES))
        .group_by(User.id, User.first_name, User.username, User.telegram_id)
        .order_by(func.sum(Order.amount).desc())
        .limit(15)
    )).all()
    top_customers = [
        {
            "user_id": str(r.id),
            "name": r.first_name or r.username or str(r.telegram_id),
            "telegram_id": int(r.telegram_id),
            "total_spent": float(r.spent or 0),
            "orders": int(r.orders or 0),
        }
        for r in rows
    ]

    # ── Revenue by plan ──
    plan_rows = (await session.execute(
        select(
            Plan.name,
            func.coalesce(func.sum(Order.amount), 0).label("revenue"),
            func.count(Order.id).label("orders"),
        )
        .join(Order, Order.plan_id == Plan.id)
        .where(Order.status.in_(_REVENUE_STATUSES))
        .group_by(Plan.id, Plan.name)
        .order_by(func.sum(Order.amount).desc())
        .limit(12)
    )).all()
    revenue_by_plan = [
        {"plan": r.name, "revenue": float(r.revenue or 0), "orders": int(r.orders or 0)}
        for r in plan_rows
    ]

    return {
        "kpis": {
            "total_revenue": round(total_revenue, 2),
            "revenue_30d": round(revenue_30d, 2),
            "revenue_7d": round(revenue_7d, 2),
            "orders_count": orders_count,
            "paying_users": paying_users,
            "arpu": round(arpu, 2),
            "avg_order_value": round(avg_order_value, 2),
        },
        "churn": {
            "active_subscribers": active_subscribers,
            "paying_users": paying_users,
            "churned_users": churned_users,
            "retention_rate": round(retention_rate, 1),
            "new_users_30d": new_users_30d,
        },
        "top_customers": top_customers,
        "revenue_by_plan": revenue_by_plan,
    }
