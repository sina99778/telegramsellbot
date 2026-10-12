"""Remind administrators about unreviewed receipts; never infer a bank deposit.

The legacy configuration key is retained so existing installs migrate to
reminders automatically. Only explicit administrator approval can credit a
card payment. Waiting longer is not evidence of payment.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.payment import Payment
from models.user import User
from repositories.settings import AppSettingsRepository
from services.notifications import notify_admins

logger = logging.getLogger(__name__)


async def run_card_autoconfirm(session: AsyncSession, bot: Bot | None = None) -> dict:
    cfg = await AppSettingsRepository(session).get_card_autoconfirm_settings()
    if not cfg.enabled or bot is None:
        return {"checked": 0, "confirmed": 0, "reminded": 0, "disabled": True}
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg.delay_minutes)
    ids = (await session.execute(
        select(Payment.id).where(
            Payment.provider == "card_to_card",
            Payment.payment_status == "pending_approval",
            Payment.callback_payload["card_review_reminded_at"].astext.is_(None),
            Payment.callback_payload["receipt_uploaded_at"].astext <= cutoff.isoformat(),
            Payment.user.has(User.telegram_id.not_in(cfg.exception_telegram_ids)),
        ).order_by(Payment.created_at).limit(100)
    )).scalars().all()
    reminded = 0
    for pid in ids:
        payment = await session.scalar(
            select(Payment).options(selectinload(Payment.user)).where(
                Payment.id == pid, Payment.payment_status == "pending_approval",
            ).with_for_update(skip_locked=True)
        )
        if payment is None:
            await session.commit()
            continue
        payload = dict(payment.callback_payload or {})
        if payload.get("card_review_reminded_at") or (
            payment.user and payment.user.telegram_id in cfg.exception_telegram_ids
        ):
            await session.commit()
            continue
        try:
            submitted = datetime.fromisoformat(str(payload.get("receipt_uploaded_at", "")))
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
        except ValueError:
            # Legacy rows have no upload time. Do not use invoice creation time.
            await session.commit()
            continue
        if submitted > cutoff:
            await session.commit()
            continue
        try:
            # Claim once under the row lock, then release it before Telegram I/O.
            payload["card_review_reminded_at"] = datetime.now(timezone.utc).isoformat()
            payment.callback_payload = payload
            await session.commit()
            await notify_admins(session, bot, f"رسید {pid} در انتظار بررسی واریز بانکی توسط مدیر است.")
            reminded += 1
        except Exception:
            logger.exception("Card review reminder failed for %s", pid)
            await session.rollback()
    return {"checked": len(ids), "confirmed": 0, "reminded": reminded}
