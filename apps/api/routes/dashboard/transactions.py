"""
Dashboard transactions + orders + pending-approvals endpoints:

    GET   /api/dashboard/transactions/orders         — orders (paginated)
    GET   /api/dashboard/transactions/payments       — payment gateway log
    GET   /api/dashboard/transactions/wallet         — wallet ledger
    GET   /api/dashboard/transactions/pending        — manual-approval queue
    GET   /api/dashboard/transactions/orders.csv     — CSV export of orders

CSV export note: streams the rows with a fixed column ordering so the
file can be opened in Excel / LibreOffice with proper UTF-8 BOM (so
Persian text doesn't garble on Windows Excel). Caps at 10 k rows per
export — that's a year of activity for a typical bot, and forces big
exports through the date-range filter rather than dumping everything.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.order import Order
from models.payment import Payment
from models.plan import Plan
from models.user import User
from models.wallet import Wallet, WalletTransaction


logger = logging.getLogger(__name__)
router = APIRouter()


AuthDep = Annotated[
    tuple[DashboardAdmin, AsyncSession],
    Depends(require_dashboard_admin),
]


_ORDER_STATUSES = ("pending", "paid", "processing", "provisioned", "refunded", "cancelled")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Orders ─────────────────────────────────────────────────────────────


@router.get("/orders")
async def list_orders(
    auth: AuthDep,
    status: str | None = Query(None, description=f"One of {_ORDER_STATUSES}"),
    user_telegram_id: int | None = Query(None),
    from_date: str | None = Query(None, description="ISO date — created_at >="),
    to_date: str | None = Query(None, description="ISO date — created_at <="),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    _admin, session = auth

    stmt = (
        select(Order)
        .options(selectinload(Order.user), selectinload(Order.plan))
        .order_by(desc(Order.created_at))
    )

    if status:
        stmt = stmt.where(Order.status == status)
    if user_telegram_id is not None:
        # Need a user lookup by tg_id → uuid.
        tg_user = await session.scalar(
            select(User.id).where(User.telegram_id == user_telegram_id)
        )
        if tg_user is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 1}
        stmt = stmt.where(Order.user_id == tg_user)
    if from_date:
        dt = _parse_iso(from_date)
        if dt is not None:
            stmt = stmt.where(Order.created_at >= dt)
    if to_date:
        dt = _parse_iso(to_date)
        if dt is not None:
            stmt = stmt.where(Order.created_at <= dt)

    total = int(await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0)

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    for o in rows:
        items.append({
            "id": str(o.id),
            "user_id": str(o.user_id),
            "user_telegram_id": int(o.user.telegram_id) if o.user else None,
            "user_first_name": o.user.first_name if o.user else None,
            "plan_name": o.plan.name if o.plan else "—",
            "amount": float(Decimal(str(o.amount or 0))),
            "currency": o.currency,
            "status": o.status,
            "source": o.source,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


@router.get("/orders.csv")
async def export_orders_csv(
    auth: AuthDep,
    status: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
) -> Response:
    """Stream the (filtered) order set as CSV, capped at 10k rows."""
    _admin, session = auth
    stmt = (
        select(Order)
        .options(selectinload(Order.user), selectinload(Order.plan))
        .order_by(desc(Order.created_at))
        .limit(10_000)
    )
    if status:
        stmt = stmt.where(Order.status == status)
    if from_date:
        dt = _parse_iso(from_date)
        if dt is not None:
            stmt = stmt.where(Order.created_at >= dt)
    if to_date:
        dt = _parse_iso(to_date)
        if dt is not None:
            stmt = stmt.where(Order.created_at <= dt)

    rows = (await session.execute(stmt)).scalars().all()

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly.
    w = csv.writer(buf)
    w.writerow([
        "order_id", "created_at", "user_telegram_id", "user_first_name",
        "plan_name", "amount", "currency", "status", "source",
    ])
    for o in rows:
        w.writerow([
            str(o.id),
            o.created_at.isoformat() if o.created_at else "",
            int(o.user.telegram_id) if o.user else "",
            (o.user.first_name or "") if o.user else "",
            o.plan.name if o.plan else "",
            float(Decimal(str(o.amount or 0))),
            o.currency,
            o.status,
            o.source,
        ])
    csv_bytes = buf.getvalue().encode("utf-8")
    headers = {"Content-Disposition": "attachment; filename=orders.csv"}
    return Response(content=csv_bytes, media_type="text/csv", headers=headers)


# ─── Payments (gateway log) ─────────────────────────────────────────────


@router.get("/payments")
async def list_payments(
    auth: AuthDep,
    status: str | None = Query(None),
    provider: str | None = Query(None),
    user_telegram_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    _admin, session = auth

    stmt = (
        select(Payment)
        .options(selectinload(Payment.user))
        .order_by(desc(Payment.created_at))
    )
    if status:
        stmt = stmt.where(Payment.payment_status == status)
    if provider:
        stmt = stmt.where(Payment.provider == provider)
    if user_telegram_id is not None:
        tg_user_id = await session.scalar(
            select(User.id).where(User.telegram_id == user_telegram_id)
        )
        if tg_user_id is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 1}
        stmt = stmt.where(Payment.user_id == tg_user_id)

    total = int(await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0)

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    for p in rows:
        items.append({
            "id": str(p.id),
            "user_telegram_id": int(p.user.telegram_id) if p.user else None,
            "user_first_name": p.user.first_name if p.user else None,
            "provider": p.provider,
            "kind": p.kind,
            "status": p.payment_status,
            "pay_currency": p.pay_currency,
            "pay_amount": float(Decimal(str(p.pay_amount or 0))) if p.pay_amount is not None else None,
            "price_currency": p.price_currency,
            "price_amount": float(Decimal(str(p.price_amount or 0))),
            "actually_paid": float(Decimal(str(p.actually_paid or 0))) if p.actually_paid is not None else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


# ─── Wallet ledger ──────────────────────────────────────────────────────


@router.get("/wallet")
async def list_wallet_txns(
    auth: AuthDep,
    user_telegram_id: int | None = Query(None),
    direction: str | None = Query(None, description="'credit' or 'debit'"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    _admin, session = auth

    stmt = (
        select(WalletTransaction)
        .options(selectinload(WalletTransaction.user))
        .order_by(desc(WalletTransaction.created_at))
    )
    if direction in ("credit", "debit"):
        stmt = stmt.where(WalletTransaction.direction == direction)
    if user_telegram_id is not None:
        tg_user_id = await session.scalar(
            select(User.id).where(User.telegram_id == user_telegram_id)
        )
        if tg_user_id is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "total_pages": 1}
        stmt = stmt.where(WalletTransaction.user_id == tg_user_id)

    total = int(await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0)

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    for t in rows:
        items.append({
            "id": str(t.id),
            "user_telegram_id": int(t.user.telegram_id) if t.user else None,
            "user_first_name": t.user.first_name if t.user else None,
            "type": t.type,
            "direction": t.direction,
            "amount": float(Decimal(str(t.amount or 0))),
            "currency": t.currency,
            "description": t.description,
            "balance_after": float(Decimal(str(t.balance_after or 0))) if t.balance_after is not None else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


# ─── Pending approvals (manual_crypto + card_to_card) ──────────────────


@router.get("/pending")
async def list_pending_approvals(auth: AuthDep) -> dict[str, Any]:
    """Payments that are sitting in admin's queue waiting for manual approval.

    Today the bot itself handles approvals through its inline buttons —
    this view is read-only so the operator can see at-a-glance how many
    invoices are awaiting attention and which user each belongs to.
    """
    _admin, session = auth

    stmt = (
        select(Payment)
        .options(selectinload(Payment.user))
        .where(
            Payment.provider.in_(("manual_crypto", "card_to_card")),
            Payment.payment_status.in_(("waiting_hash", "waiting_receipt", "pending_approval")),
        )
        .order_by(Payment.created_at.asc())  # oldest first — fairness
        .limit(100)
    )
    rows = (await session.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    for p in rows:
        items.append({
            "id": str(p.id),
            "user_telegram_id": int(p.user.telegram_id) if p.user else None,
            "user_first_name": p.user.first_name if p.user else None,
            "provider": p.provider,
            "kind": p.kind,
            "status": p.payment_status,
            "pay_currency": p.pay_currency,
            "pay_amount": float(Decimal(str(p.pay_amount or 0))) if p.pay_amount is not None else None,
            "price_amount": float(Decimal(str(p.price_amount or 0))),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })

    return {"items": items, "total": len(items)}
