"""
Dashboard plans CRUD:

    GET    /api/dashboard/plans               — list with sale counts
    POST   /api/dashboard/plans               — create
    PATCH  /api/dashboard/plans/{id}          — edit
    DELETE /api/dashboard/plans/{id}          — delete (only if zero subs)

A plan deletion that's referenced by existing subscriptions is REFUSED
with a 400 — the operator should toggle `is_active=false` instead so
the plan disappears from buy-screens but historical orders keep their
plan_name. (Identical safety to how server deletion works.)
"""
from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.plan import Plan
from models.subscription import Subscription
from models.xui import XUIInboundRecord, XUIServerRecord
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository


logger = logging.getLogger(__name__)
router = APIRouter()

AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]


@router.get("")
async def list_plans(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    settings_repo = AppSettingsRepository(session)
    # Surface the global currency mode + Toman rate so the UI can
    # label prices correctly and the create form can convert Toman
    # input → USD before submitting (internal storage is always USD).
    display_currency = await settings_repo.get_display_currency()
    toman_rate = int(await settings_repo.get_toman_rate())
    plans = (await session.execute(
        select(Plan)
        .options(
            selectinload(Plan.inbound).selectinload(XUIInboundRecord.server),
        )
        .order_by(Plan.is_active.desc(), Plan.price.asc())
    )).scalars().all()

    items: list[dict[str, Any]] = []
    for p in plans:
        sub_count = int(await session.scalar(
            select(func.count(Subscription.id)).where(Subscription.plan_id == p.id)
        ) or 0)
        server_name = None
        inbound_label = None
        if p.inbound:
            inbound_label = p.inbound.remark or f"#{p.inbound.xui_inbound_remote_id}"
            if p.inbound.server:
                server_name = p.inbound.server.name
        items.append({
            "id": str(p.id),
            "code": p.code,
            "name": p.name,
            "protocol": p.protocol,
            "duration_days": p.duration_days,
            "volume_bytes": int(p.volume_bytes),
            "volume_gb": float(p.volume_bytes) / (1024**3) if p.volume_bytes else 0.0,
            "price": float(Decimal(str(p.price))),
            "renewal_price": float(Decimal(str(p.renewal_price))),
            "currency": p.currency,
            "is_active": p.is_active,
            "ip_limit": int(p.ip_limit) if p.ip_limit is not None else None,
            "renewal_price_per_gb": float(p.renewal_price_per_gb) if p.renewal_price_per_gb is not None else None,
            "renewal_price_per_day": float(p.renewal_price_per_day) if p.renewal_price_per_day is not None else None,
            "inbound_id": str(p.inbound_id) if p.inbound_id else None,
            "inbound_label": inbound_label,
            "server_name": server_name,
            "subscription_count": sub_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {
        "items": items,
        "total": len(items),
        "display_currency": display_currency,
        "toman_rate": toman_rate,
    }


class PlanCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    protocol: str = Field("vless", min_length=1, max_length=32)
    inbound_id: UUID | None = None
    duration_days: int = Field(..., ge=1, le=3650)
    volume_gb: float = Field(..., ge=0)
    price: float = Field(..., ge=0)
    renewal_price: float | None = Field(None, ge=0)
    currency: str = Field("USD", min_length=1, max_length=16)
    # New per-plan overrides — all optional. NULL keeps the global default.
    ip_limit: int | None = Field(None, ge=0, le=1000)
    renewal_price_per_gb: float | None = Field(None, ge=0)
    renewal_price_per_day: float | None = Field(None, ge=0)


@router.post("")
async def create_plan(body: PlanCreateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth

    if body.inbound_id is not None:
        ok = await session.scalar(select(XUIInboundRecord.id).where(XUIInboundRecord.id == body.inbound_id))
        if ok is None:
            raise HTTPException(status_code=400, detail="اینباند انتخاب‌شده وجود ندارد.")

    # Auto code so the operator doesn't have to think one up.
    code = f"plan_{secrets.token_hex(4)}"
    plan = Plan(
        code=code,
        name=body.name,
        protocol=body.protocol,
        inbound_id=body.inbound_id,
        duration_days=body.duration_days,
        volume_bytes=int(body.volume_gb * 1024**3),
        price=Decimal(str(body.price)),
        renewal_price=Decimal(str(body.renewal_price if body.renewal_price is not None else body.price)),
        currency=body.currency,
        is_active=True,
        ip_limit=body.ip_limit,
        renewal_price_per_gb=Decimal(str(body.renewal_price_per_gb)) if body.renewal_price_per_gb is not None else None,
        renewal_price_per_day=Decimal(str(body.renewal_price_per_day)) if body.renewal_price_per_day is not None else None,
    )
    session.add(plan)
    await session.flush()

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_plan_create",
            entity_type="plan",
            entity_id=plan.id,
            payload={"dashboard_admin": admin.username, "code": code, "name": body.name, "price": float(body.price)},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "id": str(plan.id), "code": code}


class PlanUpdateBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    protocol: str | None = Field(None, min_length=1, max_length=32)
    inbound_id: UUID | None = None
    duration_days: int | None = Field(None, ge=1, le=3650)
    volume_gb: float | None = Field(None, ge=0)
    price: float | None = Field(None, ge=0)
    renewal_price: float | None = Field(None, ge=0)
    currency: str | None = Field(None, min_length=1, max_length=16)
    is_active: bool | None = None
    # Per-plan overrides. To CLEAR an override back to "use global", send -1.
    # (Sending null in PATCH means "don't touch", which is the standard PATCH
    # semantic — but here clients want a way to actively unset the field.)
    ip_limit: int | None = Field(None, ge=-1, le=1000)
    renewal_price_per_gb: float | None = Field(None, ge=-1)
    renewal_price_per_day: float | None = Field(None, ge=-1)


@router.patch("/{plan_id}")
async def update_plan(plan_id: UUID, body: PlanUpdateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    plan = await session.scalar(select(Plan).where(Plan.id == plan_id))
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن یافت نشد.")

    changes: dict[str, Any] = {}
    if body.name is not None:           plan.name = body.name; changes["name"] = body.name
    if body.protocol is not None:       plan.protocol = body.protocol; changes["protocol"] = body.protocol
    if body.duration_days is not None:  plan.duration_days = body.duration_days; changes["duration_days"] = body.duration_days
    if body.volume_gb is not None:
        plan.volume_bytes = int(body.volume_gb * 1024**3); changes["volume_gb"] = body.volume_gb
    if body.price is not None:          plan.price = Decimal(str(body.price)); changes["price"] = body.price
    if body.renewal_price is not None:  plan.renewal_price = Decimal(str(body.renewal_price)); changes["renewal_price"] = body.renewal_price
    if body.currency is not None:       plan.currency = body.currency; changes["currency"] = body.currency
    if body.is_active is not None:      plan.is_active = body.is_active; changes["is_active"] = body.is_active

    # Per-plan overrides. -1 is the "unset" sentinel (PATCH null already
    # means "don't touch"), so dashboard can actively roll back to the
    # global default.
    if body.ip_limit is not None:
        plan.ip_limit = None if body.ip_limit < 0 else int(body.ip_limit)
        changes["ip_limit"] = plan.ip_limit
    if body.renewal_price_per_gb is not None:
        plan.renewal_price_per_gb = None if body.renewal_price_per_gb < 0 else Decimal(str(body.renewal_price_per_gb))
        changes["renewal_price_per_gb"] = float(plan.renewal_price_per_gb) if plan.renewal_price_per_gb is not None else None
    if body.renewal_price_per_day is not None:
        plan.renewal_price_per_day = None if body.renewal_price_per_day < 0 else Decimal(str(body.renewal_price_per_day))
        changes["renewal_price_per_day"] = float(plan.renewal_price_per_day) if plan.renewal_price_per_day is not None else None

    # Inbound change requires existence check + invalidates the cached
    # client mappings on the bot side. Don't bother changing it for
    # plans that already have customers — operator should use the
    # "pivot all plans to inbound" feature instead.
    if body.inbound_id is not None:
        sub_count = int(await session.scalar(
            select(func.count(Subscription.id)).where(Subscription.plan_id == plan.id)
        ) or 0)
        if sub_count > 0 and body.inbound_id != plan.inbound_id:
            raise HTTPException(
                status_code=400,
                detail=f"این پلن {sub_count} سرویس فعال دارد — برای تغییر inbound از «انتقال همه‌ی پلن‌ها» در پنل ادمین بات استفاده کن.",
            )
        ok = await session.scalar(select(XUIInboundRecord.id).where(XUIInboundRecord.id == body.inbound_id))
        if ok is None:
            raise HTTPException(status_code=400, detail="اینباند انتخاب‌شده وجود ندارد.")
        plan.inbound_id = body.inbound_id; changes["inbound_id"] = str(body.inbound_id)

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_plan_update",
            entity_type="plan",
            entity_id=plan.id,
            payload={"dashboard_admin": admin.username, "changes": changes},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


@router.delete("/{plan_id}")
async def delete_plan(plan_id: UUID, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    plan = await session.scalar(select(Plan).where(Plan.id == plan_id))
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن یافت نشد.")

    sub_count = int(await session.scalar(
        select(func.count(Subscription.id)).where(Subscription.plan_id == plan.id)
    ) or 0)
    if sub_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"این پلن {sub_count} سرویس دارد. به‌جای حذف، آن را غیرفعال کن.",
        )
    await session.delete(plan)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_plan_delete",
            entity_type="plan",
            entity_id=plan.id,
            payload={"dashboard_admin": admin.username, "code": plan.code},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


# ─── Inbound picker (used by Plans create/edit) ──────────────────────────


@router.get("/_inbounds")
async def list_inbounds_for_picker(auth: AuthDep) -> dict[str, Any]:
    """Active inbounds across active servers, formatted for a select box."""
    _admin, session = auth
    rows = (await session.execute(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.is_active.is_(True))
        .order_by(XUIInboundRecord.created_at.desc())
    )).scalars().all()
    items: list[dict[str, Any]] = []
    for ib in rows:
        if not ib.server or not ib.server.is_active:
            continue
        items.append({
            "id": str(ib.id),
            "label": f"{ib.server.name} → {ib.remark or '#' + str(ib.xui_inbound_remote_id)} ({ib.protocol or '?'}:{ib.port or '?'})",
        })
    return {"items": items}
