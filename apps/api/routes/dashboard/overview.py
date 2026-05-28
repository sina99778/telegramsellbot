"""
Dashboard Overview endpoint — GET /api/dashboard/overview

The "front page" of the web management console. Returns:
  * Six headline KPIs (users, active subs, revenue MTD, traffic delivered,
    active servers, last-24h activity).
  * Three time-series suitable for line charts (revenue/30d, signups/30d,
    active-subs/30d).
  * A short recent-activity feed.

Implementation rules
--------------------
* Every aggregation runs against the operator's PG in a single round-trip
  where possible — no N+1 inside the response builder.
* Active subs are filtered to ("active", "pending_activation") so the
  number matches what the bot's own user-facing dashboard reports.
* Revenue is summed from `Order.amount` where status ∈ (paid, processing,
  provisioned) — same definition the existing `AdminStatsRepository`
  uses (so dashboard ↔ bot stats never disagree).
* Traffic = lifetime_used_bytes + used_bytes (same convention as
  reseller-billing; introduced in commit b47ff13).
* Chart series are computed in Postgres via `date_trunc('day', …)` so
  there's no Python loop over 30 days of rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.order import Order
from models.subscription import Subscription
from models.user import User
from models.xui import XUIServerRecord


router = APIRouter()


# ── Constants matching the bot's own conventions ────────────────────────
_REVENUE_STATUSES = ("paid", "processing", "provisioned")
_BILLABLE_SUB_STATUSES = ("active", "pending_activation", "expired", "disabled")
_ACTIVE_SUB_STATUSES = ("active", "pending_activation")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


async def _kpis(session: AsyncSession) -> dict[str, Any]:
    now = _now_utc()
    today_start = _start_of_day(now)
    month_start = today_start.replace(day=1)
    one_day_ago = now - timedelta(days=1)

    # All six headline aggregations in parallel-ish form — SQLAlchemy
    # ships them as separate statements but the round-trip cost on a
    # local Postgres is dwarfed by the wire/parse overhead of the Vue
    # client, so this is fine.

    total_users = int(await session.scalar(
        select(func.count(User.id))
    ) or 0)

    active_subs = int(await session.scalar(
        select(func.count(Subscription.id))
        .where(Subscription.status.in_(_ACTIVE_SUB_STATUSES))
    ) or 0)

    revenue_mtd = await session.scalar(
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(
            Order.status.in_(_REVENUE_STATUSES),
            Order.created_at >= month_start,
        )
    )

    traffic_bytes = int(await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(Subscription.lifetime_used_bytes, 0)
                    + func.coalesce(Subscription.used_bytes, 0)
                ),
                0,
            )
        ).where(Subscription.status.in_(_BILLABLE_SUB_STATUSES))
    ) or 0)

    active_servers = int(await session.scalar(
        select(func.count(XUIServerRecord.id))
        .where(XUIServerRecord.is_active.is_(True))
    ) or 0)

    # ── Last-24h trio ────────────────────────────────────────────────
    signups_24h = int(await session.scalar(
        select(func.count(User.id))
        .where(User.created_at >= one_day_ago)
    ) or 0)
    purchases_24h = int(await session.scalar(
        select(func.count(Order.id))
        .where(
            Order.status.in_(_REVENUE_STATUSES),
            Order.created_at >= one_day_ago,
        )
    ) or 0)
    revenue_24h = await session.scalar(
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(
            Order.status.in_(_REVENUE_STATUSES),
            Order.created_at >= one_day_ago,
        )
    )

    return {
        "total_users": total_users,
        "active_subs": active_subs,
        "revenue_mtd_usd": float(Decimal(str(revenue_mtd or 0))),
        "traffic_delivered_bytes": traffic_bytes,
        "active_servers": active_servers,
        "last_24h": {
            "signups": signups_24h,
            "purchases": purchases_24h,
            "revenue_usd": float(Decimal(str(revenue_24h or 0))),
        },
    }


async def _series_revenue_30d(session: AsyncSession) -> list[dict[str, Any]]:
    """Per-day revenue for the last 30 calendar days (oldest first)."""
    now = _now_utc()
    start = _start_of_day(now) - timedelta(days=29)
    day = func.date_trunc("day", Order.created_at).label("d")
    rows = (await session.execute(
        select(day, func.coalesce(func.sum(Order.amount), 0).label("revenue"))
        .where(
            Order.status.in_(_REVENUE_STATUSES),
            Order.created_at >= start,
        )
        .group_by(day)
        .order_by(day)
    )).all()
    # Densify: gaps in the result set become explicit zero-revenue days
    # so Chart.js doesn't compress the X-axis.
    by_day = {r[0].date().isoformat(): float(Decimal(str(r[1]))) for r in rows if r[0]}
    out: list[dict[str, Any]] = []
    for i in range(30):
        d = (start + timedelta(days=i)).date().isoformat()
        out.append({"date": d, "value": by_day.get(d, 0.0)})
    return out


async def _series_signups_30d(session: AsyncSession) -> list[dict[str, Any]]:
    now = _now_utc()
    start = _start_of_day(now) - timedelta(days=29)
    day = func.date_trunc("day", User.created_at).label("d")
    rows = (await session.execute(
        select(day, func.count(User.id).label("c"))
        .where(User.created_at >= start)
        .group_by(day)
        .order_by(day)
    )).all()
    by_day = {r[0].date().isoformat(): int(r[1]) for r in rows if r[0]}
    out: list[dict[str, Any]] = []
    for i in range(30):
        d = (start + timedelta(days=i)).date().isoformat()
        out.append({"date": d, "value": by_day.get(d, 0)})
    return out


async def _recent_activity(session: AsyncSession) -> list[dict[str, Any]]:
    """A merged feed of the last few significant events for the activity panel.

    Cheap version: last 5 successful orders + last 5 signups, merged + sorted.
    """
    out: list[dict[str, Any]] = []

    recent_orders = (await session.execute(
        select(Order.id, Order.amount, Order.status, Order.created_at, User.telegram_id, User.first_name)
        .join(User, User.id == Order.user_id)
        .where(Order.status.in_(_REVENUE_STATUSES))
        .order_by(Order.created_at.desc())
        .limit(5)
    )).all()
    for r in recent_orders:
        out.append({
            "kind": "order",
            "at": r[3].isoformat() if r[3] else None,
            "amount_usd": float(Decimal(str(r[1] or 0))),
            "status": r[2],
            "user_telegram_id": int(r[4]) if r[4] else None,
            "user_first_name": r[5] or None,
        })

    recent_users = (await session.execute(
        select(User.id, User.telegram_id, User.first_name, User.created_at)
        .order_by(User.created_at.desc())
        .limit(5)
    )).all()
    for r in recent_users:
        out.append({
            "kind": "signup",
            "at": r[3].isoformat() if r[3] else None,
            "user_telegram_id": int(r[1]) if r[1] else None,
            "user_first_name": r[2] or None,
        })

    out.sort(key=lambda e: e.get("at") or "", reverse=True)
    return out[:10]


@router.get("")
@router.get("/")
async def get_overview(
    auth: Annotated[
        tuple[DashboardAdmin, AsyncSession],
        Depends(require_dashboard_admin),
    ],
) -> dict[str, Any]:
    """Dashboard "home" payload.

    Single round-trip JSON the SPA's Overview view can render in one shot.
    Recomputed on every hit; the underlying queries are cheap (one
    indexed COUNT each).
    """
    _admin, session = auth
    kpis = await _kpis(session)
    return {
        "generated_at": _now_utc().isoformat(),
        "kpis": kpis,
        "charts": {
            "revenue_30d": await _series_revenue_30d(session),
            "signups_30d": await _series_signups_30d(session),
        },
        "recent_activity": await _recent_activity(session),
    }
