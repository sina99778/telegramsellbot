"""
Dashboard discount-codes CRUD:

    GET    /api/dashboard/discounts
    POST   /api/dashboard/discounts
    PATCH  /api/dashboard/discounts/{id}
    DELETE /api/dashboard/discounts/{id}
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.discount import DiscountCode
from repositories.audit import AuditLogRepository


logger = logging.getLogger(__name__)
router = APIRouter()
AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]


def _to_dict(d: DiscountCode) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "code": d.code,
        "discount_percent": int(d.discount_percent),
        "max_uses": int(d.max_uses),
        "used_count": int(d.used_count),
        "is_active": bool(d.is_active),
        "expires_at": d.expires_at.isoformat() if d.expires_at else None,
        "plan_id": str(d.plan_id) if d.plan_id else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.get("")
async def list_discounts(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    rows = (await session.execute(
        select(DiscountCode).order_by(desc(DiscountCode.created_at))
    )).scalars().all()
    return {"items": [_to_dict(d) for d in rows], "total": len(rows)}


class DiscountCreateBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    discount_percent: int = Field(..., ge=1, le=100)
    max_uses: int = Field(1, ge=1, le=1000000)
    expires_at: str | None = None
    plan_id: UUID | None = None


@router.post("")
async def create_discount(body: DiscountCreateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    code_clean = body.code.strip().upper()
    if not code_clean:
        raise HTTPException(status_code=400, detail="کد تخفیف خالی است.")
    dup = await session.scalar(select(DiscountCode.id).where(DiscountCode.code == code_clean))
    if dup is not None:
        raise HTTPException(status_code=400, detail="کد تخفیف تکراری.")

    expires = None
    if body.expires_at:
        try:
            expires = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="فرمت تاریخ انقضا معتبر نیست.")

    dc = DiscountCode(
        code=code_clean,
        discount_percent=body.discount_percent,
        max_uses=body.max_uses,
        expires_at=expires,
        plan_id=body.plan_id,
        is_active=True,
    )
    session.add(dc)
    await session.flush()
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_discount_create",
            entity_type="discount",
            entity_id=dc.id,
            payload={"dashboard_admin": admin.username, "code": code_clean, "percent": body.discount_percent},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "id": str(dc.id)}


class DiscountUpdateBody(BaseModel):
    discount_percent: int | None = Field(None, ge=1, le=100)
    max_uses: int | None = Field(None, ge=1, le=1000000)
    is_active: bool | None = None
    expires_at: str | None = None
    plan_id: UUID | None = None


@router.patch("/{discount_id}")
async def update_discount(discount_id: UUID, body: DiscountUpdateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    dc = await session.scalar(select(DiscountCode).where(DiscountCode.id == discount_id))
    if dc is None:
        raise HTTPException(status_code=404, detail="کد تخفیف یافت نشد.")
    changes: dict[str, Any] = {}
    if body.discount_percent is not None: dc.discount_percent = body.discount_percent; changes["discount_percent"] = body.discount_percent
    if body.max_uses is not None:         dc.max_uses = body.max_uses; changes["max_uses"] = body.max_uses
    if body.is_active is not None:        dc.is_active = body.is_active; changes["is_active"] = body.is_active
    if body.plan_id is not None:          dc.plan_id = body.plan_id;   changes["plan_id"] = str(body.plan_id)
    if body.expires_at is not None:
        if body.expires_at == "":
            dc.expires_at = None
        else:
            try:
                dc.expires_at = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="فرمت تاریخ نامعتبر.")
        changes["expires_at"] = body.expires_at
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_discount_update",
            entity_type="discount",
            entity_id=dc.id,
            payload={"dashboard_admin": admin.username, "changes": changes},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


@router.delete("/{discount_id}")
async def delete_discount(discount_id: UUID, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    dc = await session.scalar(select(DiscountCode).where(DiscountCode.id == discount_id))
    if dc is None:
        raise HTTPException(status_code=404, detail="کد تخفیف یافت نشد.")
    await session.delete(dc)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_discount_delete",
            entity_type="discount",
            entity_id=dc.id,
            payload={"dashboard_admin": admin.username, "code": dc.code},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}
