"""
Dashboard broadcast composer — schedules a BroadcastJob row that the
existing worker (apps/worker/jobs/broadcast.py) picks up on its 20s
poll. Read-only list of recent jobs + a POST to queue a new one.

    GET   /api/dashboard/broadcast              — recent jobs (last 20)
    POST  /api/dashboard/broadcast              — queue a new text broadcast

For now we only support plain-text broadcasts via the dashboard
(operator can still use the bot's own /admin → broadcast flow for
photo / forwarded media). The audience is "every user" — the worker
filters out blocked / banned users automatically.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.broadcast import BroadcastJob
from models.dashboard_admin import DashboardAdmin
from models.user import User
from repositories.audit import AuditLogRepository


logger = logging.getLogger(__name__)
router = APIRouter()
AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]


# ─── List ────────────────────────────────────────────────────────────────


@router.get("")
async def list_broadcasts(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    rows = (await session.execute(
        select(BroadcastJob).order_by(desc(BroadcastJob.created_at)).limit(20)
    )).scalars().all()
    items: list[dict[str, Any]] = []
    for j in rows:
        items.append({
            "id": str(j.id),
            "status": j.status,
            "message_type": j.message_type,
            "text_preview": (j.text or "")[:200],
            "total": int(j.total_recipients),
            "processed": int(j.processed_recipients),
            "failed": int(j.failed_recipients),
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "via": str(j.payload.get("via") if j.payload else "") or "bot",
        })
    total_users = int(await session.scalar(select(func.count(User.id))) or 0)
    return {"items": items, "total": len(items), "total_users": total_users}


# ─── Create ──────────────────────────────────────────────────────────────


class BroadcastCreateBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    audience: str = Field("all", pattern="^(all|active|inactive)$",
                          description="all = every user; active = has any active sub; inactive = no active sub")


@router.post("")
async def create_broadcast(body: BroadcastCreateBody, auth: AuthDep) -> dict[str, Any]:
    """Queue a broadcast job. The worker (broadcast.py) picks it up on
    its next 20s poll, sends in batches, throttles to avoid Telegram's
    rate limits, and stamps `finished_at` when done."""
    admin, session = auth

    # BroadcastJob.created_by_user_id is non-null and FKs to users.id.
    # Dashboard admins live in a separate table, so we attribute the
    # job to the first bot-side admin/owner we find — the actual
    # dashboard-admin username goes in payload for audit.
    actor_user_id = await session.scalar(
        select(User.id).where(User.role.in_(("admin", "owner"))).limit(1)
    )
    if actor_user_id is None:
        raise HTTPException(
            status_code=500,
            detail="هیچ کاربر admin/owner در DB پیدا نشد — یک کاربر را به ادمین تبدیل کن.",
        )

    job = BroadcastJob(
        created_by_user_id=actor_user_id,
        status="queued",
        message_type="text",
        text=body.text,
        payload={"via": "dashboard", "dashboard_admin": admin.username, "audience": body.audience},
    )
    session.add(job)
    await session.flush()
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_broadcast_create",
            entity_type="broadcast",
            entity_id=job.id,
            payload={
                "dashboard_admin": admin.username,
                "audience": body.audience,
                "preview": body.text[:120],
            },
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "id": str(job.id)}
