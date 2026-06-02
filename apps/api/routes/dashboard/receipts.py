"""
Dashboard pending-card-receipts queue.

Endpoints
---------
    GET    /api/dashboard/receipts              — list pending card receipts
    GET    /api/dashboard/receipts/{id}         — detail (incl. risk context)
    GET    /api/dashboard/receipts/{id}/photo   — proxy the receipt photo
                                                  bytes from Telegram so the
                                                  SPA can show it inline
                                                  without leaking the bot token
    POST   /api/dashboard/receipts/{id}/approve — confirm + credit wallet
    POST   /api/dashboard/receipts/{id}/reject  — mark rejected (no credit)

Currently a card-to-card receipt only lives in admin DMs in the bot. This
queue gives the operator a single web view to triage every pending
receipt in one go, with the photo right next to the risk-context block
(account age, past rejections, lifetime paid amount). Same audit trail
as the bot-side flow.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.routes.dashboard._deps import require_dashboard_admin
from core.config import settings
from models.dashboard_admin import DashboardAdmin
from models.payment import Payment
from models.user import User
from repositories.audit import AuditLogRepository
from services.payment import process_successful_payment


logger = logging.getLogger(__name__)
router = APIRouter()
AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]


def _serialize(p: Payment, user: User | None) -> dict[str, Any]:
    payload = dict(p.callback_payload or {})
    return {
        "id": str(p.id),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "price_amount_usd": float(p.price_amount or 0),
        "pay_amount": float(p.pay_amount or 0),
        "pay_currency": p.pay_currency,
        "status": p.payment_status,
        "receipt_file_id": payload.get("receipt_file_id") or p.provider_payment_id,
        "card_number": payload.get("card_number"),
        "card_holder": payload.get("card_holder"),
        "card_bank": payload.get("card_bank"),
        "user": (
            {
                "id": str(user.id),
                "telegram_id": int(user.telegram_id),
                "first_name": user.first_name,
                "username": user.username,
            } if user else None
        ),
    }


@router.get("")
async def list_pending_receipts(auth: AuthDep, status: str = "pending") -> dict[str, Any]:
    """List card-to-card receipts.

    ?status= controls the view:
      * "pending"  (default) — only awaiting approval (the live queue)
      * "approved" — already credited (finished)
      * "rejected" — rejected
      * "history"  — approved + rejected (everything already decided)
      * "all"      — every card receipt regardless of status

    The photo proxy works for ANY status, so the operator can re-open a
    receipt they already approved/rejected (e.g. after a misclick).
    """
    _admin, session = auth
    status = (status or "pending").lower()
    stmt = (
        select(Payment)
        .options(selectinload(Payment.user))
        .where(Payment.provider == "card_to_card")
    )
    if status == "pending":
        stmt = stmt.where(Payment.payment_status == "pending_approval")
    elif status == "approved":
        stmt = stmt.where(Payment.payment_status == "finished")
    elif status == "rejected":
        stmt = stmt.where(Payment.payment_status == "rejected")
    elif status == "history":
        stmt = stmt.where(Payment.payment_status.in_(("finished", "rejected")))
    # status == "all" → no extra status filter

    rows = (await session.execute(
        stmt.order_by(desc(Payment.created_at)).limit(200)
    )).scalars().all()
    items = [_serialize(p, p.user) for p in rows]
    return {"items": items, "total": len(items)}


@router.get("/{payment_id}")
async def receipt_detail(payment_id: UUID, auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    payment = await session.scalar(
        select(Payment).options(selectinload(Payment.user)).where(Payment.id == payment_id)
    )
    if payment is None or payment.provider != "card_to_card":
        raise HTTPException(status_code=404, detail="receipt not found")
    user: User | None = payment.user
    base = _serialize(payment, user)

    # Risk context — same fields the bot's _build_payment_context computes.
    context: dict[str, Any] = {"account_age_days": None, "rejected_recent": 0, "paid_lifetime": 0}
    if user:
        created = user.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created:
            context["account_age_days"] = (datetime.now(timezone.utc) - created).days
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        context["rejected_recent"] = int(await session.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.user_id == user.id,
                Payment.payment_status.in_(("rejected", "failed", "expired")),
                Payment.created_at >= cutoff,
            )
        ) or 0)
        context["paid_lifetime"] = int(await session.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.user_id == user.id,
                Payment.payment_status == "finished",
            )
        ) or 0)
    base["context"] = context

    # OCR fraud verdict — does the operator's own card appear on the receipt?
    base["ocr"] = None
    payload = dict(payment.callback_payload or {})
    file_id = payload.get("receipt_file_id") or payment.provider_payment_id
    if file_id:
        try:
            from services.receipt_ocr import assess_card_receipt
            v = await assess_card_receipt(
                str(file_id),
                card_number=payload.get("card_number"),
                card_holder=payload.get("card_holder"),
                expected_toman=int(payment.pay_amount or 0) or None,
            )
            summary = (v.get("summary") or "").replace("<b>", "").replace("</b>", "")
            base["ocr"] = {"ok": v.get("ok"), "summary": summary}
        except Exception as exc:  # noqa: BLE001
            logger.warning("receipt OCR for dashboard failed: %s", exc)
    return base


@router.get("/{payment_id}/photo")
async def receipt_photo(payment_id: UUID, auth: AuthDep) -> Response:
    """Stream the receipt photo from Telegram → SPA.

    We use the bot token here (server-side) so it never reaches the
    browser. Telegram returns a file path under /file/bot<token>/<path>;
    we proxy the bytes through with caching headers for the SPA.
    """
    _admin, session = auth
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.provider != "card_to_card":
        raise HTTPException(status_code=404, detail="receipt not found")
    payload = dict(payment.callback_payload or {})
    file_id = payload.get("receipt_file_id") or payment.provider_payment_id
    if not file_id:
        raise HTTPException(status_code=404, detail="no photo on receipt")

    token = settings.bot_token.get_secret_value()
    if not token or token == "CHANGE_ME":
        raise HTTPException(status_code=500, detail="bot token not configured")

    # Two-step Telegram protocol: getFile → /file/bot<token>/<path>.
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id},
            )
            r.raise_for_status()
            file_path = (r.json() or {}).get("result", {}).get("file_path")
        except Exception as exc:
            logger.warning("getFile failed for payment %s: %s", payment_id, exc)
            raise HTTPException(status_code=502, detail="telegram getFile failed") from exc
        if not file_path:
            raise HTTPException(status_code=502, detail="telegram returned no file path")

        try:
            file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
            file_resp = await client.get(file_url)
            file_resp.raise_for_status()
        except Exception as exc:
            logger.warning("file fetch failed for payment %s: %s", payment_id, exc)
            raise HTTPException(status_code=502, detail="telegram file fetch failed") from exc

    content_type = file_resp.headers.get("content-type", "image/jpeg")
    return StreamingResponse(
        io.BytesIO(file_resp.content),
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/{payment_id}/approve")
async def approve_receipt(payment_id: UUID, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.provider != "card_to_card":
        raise HTTPException(status_code=404, detail="receipt not found")
    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        raise HTTPException(
            status_code=400,
            detail=f"payment already processed (status={payment.payment_status})",
        )
    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=payment.price_amount,
        )
    except Exception as exc:
        logger.error("dashboard approve_receipt failed for %s: %s", payment_id, exc, exc_info=True)
        # Generic message — the raw exception (which can leak DB/internal detail)
        # is in the server log, not the HTTP response.
        raise HTTPException(status_code=500, detail="تأیید رسید ناموفق بود (خطای داخلی).") from exc

    # Stamp the dashboard admin's identity on the payload so the bot side
    # can tell that the approval came from the web UI.
    payload = dict(payment.callback_payload or {})
    payload["approved_by_dashboard_admin"] = admin.username
    payload["approved_at"] = datetime.now(timezone.utc).isoformat()
    payment.callback_payload = payload

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_receipt_approve",
            entity_type="payment",
            entity_id=payment.id,
            payload={"dashboard_admin": admin.username, "amount_usd": float(payment.price_amount or 0)},
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


@router.post("/{payment_id}/reject")
async def reject_receipt(payment_id: UUID, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.provider != "card_to_card":
        raise HTTPException(status_code=404, detail="receipt not found")
    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        raise HTTPException(
            status_code=400,
            detail=f"payment already processed (status={payment.payment_status})",
        )
    payment.payment_status = "rejected"
    payload = dict(payment.callback_payload or {})
    payload["rejected_by_dashboard_admin"] = admin.username
    payload["rejected_at"] = datetime.now(timezone.utc).isoformat()
    payment.callback_payload = payload
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_receipt_reject",
            entity_type="payment",
            entity_id=payment.id,
            payload={"dashboard_admin": admin.username},
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}
