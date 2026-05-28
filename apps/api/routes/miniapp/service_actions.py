"""
Mini-app per-service actions.

Faoxima parity for `api/handlers/ServiceActionHandler.php` — the user
can request various actions on a subscription right from the mini-app
without going through the bot menus.

    POST  /api/miniapp/services/{sub_id}/note          — set user note
    POST  /api/miniapp/services/{sub_id}/report        — report an issue
                                                          (opens a ticket)
    POST  /api/miniapp/services/{sub_id}/action_request — request a
                                                          structured admin
                                                          action: refund,
                                                          change_link,
                                                          transfer, etc.

All write paths require the same Telegram-initData auth as the rest of
the mini-app endpoints and verify subscription ownership.

`action_request` is intentionally generic: it stores the user's intent
on an AuditLog row tagged `service_action_request` with
`entity_type=subscription`, `entity_id=<sub_id>`, payload carrying
`{type, details, user_telegram_id}`. The dashboard's existing audit
view + a new "requests" filter (follow-up commit) can surface them to
the operator for triage. For now the bot DMs all admins with
approve / reject inline buttons so the operator doesn't miss them.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.miniapp.users import _get_current_user
from core.config import settings
from models.subscription import Subscription
from models.ticket import Ticket, TicketMessage
from models.user import User
from repositories.audit import AuditLogRepository


logger = logging.getLogger(__name__)
router = APIRouter()


VALID_ACTION_TYPES = {
    "refund",            # request refund
    "change_link",       # request fresh sub_link / UUID rotation
    "transfer",          # transfer service to another telegram_id
    "change_location",   # request migration to another inbound
    "toggle_disable",    # ask admin to temporarily disable
}


async def _load_subscription(session: AsyncSession, user: User, sub_id: UUID) -> Subscription:
    sub = await session.scalar(
        select(Subscription).where(Subscription.id == sub_id, Subscription.user_id == user.id)
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="subscription not found")
    return sub


# ─── Note ─────────────────────────────────────────────────────────


class NoteBody(BaseModel):
    note: str = Field("", max_length=256)


@router.post("/services/{sub_id}/note")
async def set_user_note(
    sub_id: UUID,
    body: NoteBody,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    sub = await _load_subscription(session, user, sub_id)
    cleaned = body.note.strip() or None
    sub.user_note = cleaned
    await session.commit()
    return {"ok": True, "user_note": cleaned}


# ─── Report issue (ticket) ────────────────────────────────────────


class ReportBody(BaseModel):
    subject: str = Field(..., min_length=4, max_length=120)
    description: str = Field(..., min_length=4, max_length=2000)


@router.post("/services/{sub_id}/report")
async def report_issue(
    sub_id: UUID,
    body: ReportBody,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    """Open a support ticket prefilled with the subscription's identifiers."""
    user, session = auth
    sub = await _load_subscription(session, user, sub_id)

    ticket = Ticket(user_id=user.id, status="open")
    session.add(ticket)
    await session.flush()

    head = (
        f"🛠 Report on subscription {sub.id}\n"
        f"Plan: {sub.plan_id or '—'}\n"
        f"Status: {sub.status}\n\n"
        f"Subject: {body.subject}\n\n"
    )
    message = TicketMessage(
        ticket_id=ticket.id,
        sender_id=user.id,
        text=head + body.description,
    )
    session.add(message)

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=user.id,
            action="service_report_issue",
            entity_type="subscription",
            entity_id=sub.id,
            payload={"ticket_id": str(ticket.id), "subject": body.subject},
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)

    await session.commit()
    return {"ok": True, "ticket_id": str(ticket.id)}


# ─── Action request (refund / change_link / transfer / …) ─────────


class ActionRequestBody(BaseModel):
    type: str = Field(..., min_length=3, max_length=32)
    details: str | None = Field(None, max_length=1000)


@router.post("/services/{sub_id}/action_request")
async def request_admin_action(
    sub_id: UUID,
    body: ActionRequestBody,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    """User-initiated request for an admin-approved action on the service.

    We don't apply anything here — we DM every admin telegram_id with
    a description + approve/reject inline keyboard. The operator
    decides what to do; the existing admin handlers (refund, change-
    link, etc.) carry out the actual change.
    """
    user, session = auth
    sub = await _load_subscription(session, user, sub_id)
    action_type = body.type.strip().lower()
    if action_type not in VALID_ACTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action type. valid: {sorted(VALID_ACTION_TYPES)}",
        )

    try:
        log = await AuditLogRepository(session).log_action(
            actor_user_id=user.id,
            action="service_action_request",
            entity_type="subscription",
            entity_id=sub.id,
            payload={
                "type": action_type,
                "details": (body.details or "").strip(),
                "user_telegram_id": int(user.telegram_id),
                "user_first_name": user.first_name,
            },
        )
    except Exception as exc:
        logger.warning("audit log failed for action request: %s", exc)
        log = None

    await session.commit()

    # Best-effort: DM the admins. Same pattern as the card-receipt
    # upload helper — spin up a short-lived bot session so the
    # operator notification fires immediately without going through
    # the worker queue.
    try:
        from aiogram.client.default import DefaultBotProperties
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from apps.bot.premium_bot import PremiumEmojiBot
        from sqlalchemy import select as _sel

        token = settings.bot_token.get_secret_value()
        if not token or token == "CHANGE_ME":
            raise RuntimeError("bot token not configured")

        admin_ids: set[int] = set()
        if settings.owner_telegram_id:
            admin_ids.add(int(settings.owner_telegram_id))
        result = await session.execute(_sel(User.telegram_id).where(User.role.in_(["admin", "owner"])))
        admin_ids.update(int(x) for x in result.scalars().all())

        bot = PremiumEmojiBot(
            token=token,
            default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
        )
        try:
            label_fa = {
                "refund": "بازگشت وجه",
                "change_link": "تغییر/تجدید لینک",
                "transfer": "انتقال به آی‌دی دیگر",
                "change_location": "تغییر لوکیشن",
                "toggle_disable": "غیرفعال‌سازی موقت",
            }.get(action_type, action_type)

            builder = InlineKeyboardBuilder()
            # We don't have routed handlers for these yet — admins do
            # the action manually then mark the audit row resolved.
            # Keeping the buttons here as no-op deep links would just
            # confuse, so for now we surface only "View user" via deep link.
            builder.button(
                text="مشاهده کاربر در بات",
                url=f"tg://user?id={user.telegram_id}",
            )
            builder.adjust(1)

            details_block = f"\n\nتوضیحات:\n{body.details}" if body.details else ""
            caption = (
                f"🔔 <b>درخواست از کاربر (مینی‌اپ)</b>\n\n"
                f"نوع: <b>{label_fa}</b>\n"
                f"کاربر: <code>{user.telegram_id}</code>\n"
                f"سرویس: <code>{sub.id}</code>\n"
                f"وضعیت سرویس: {sub.status}{details_block}"
            )
            for admin_id in admin_ids:
                try:
                    await bot.send_message(admin_id, caption, reply_markup=builder.as_markup())
                except Exception:
                    continue
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.warning("admin notify after action_request failed: %s", exc)

    return {"ok": True, "audit_id": str(log.id) if log else None}
