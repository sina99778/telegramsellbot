"""
Worker job: auto-approve card-receipt payments that have been sitting in
`pending_approval` longer than the operator-configured delay.

Why
---
Manual card-to-card top-ups land in `pending_approval` after the user
uploads a receipt photo. Without this job, an admin has to look at every
single receipt and tap "approve" — which doesn't scale once volume picks
up. The Faoxima bot solved this by auto-approving after a configurable
timeout, with an exception list for users who must always be reviewed
manually (high-risk or chargeback-history accounts).

Runs every 60 s. For each payment whose status is `pending_approval`
and whose `created_at` is older than `delay_minutes`:
    1) Skip if user.telegram_id is on the exception list.
    2) Call services.payment.process_successful_payment, which credits
       the wallet through the same idempotent path the manual-approval
       handler uses. (No special-cased logic — keeps the audit story
       consistent.)
    3) Log + post to the sales-report channel.

Safety
------
* Wrapped in `session.begin_nested()` per payment so one failure doesn't
  roll back already-confirmed siblings.
* `with_for_update(skip_locked=True)` honours the lock contract on
  `process_successful_payment` (same pattern as crypto_autoconfirm).
* The exception list is checked at the User level (not Payment), so an
  exempt user is never auto-approved even if they have many pending
  receipts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape as _esc

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.payment import Payment
from models.user import User
from repositories.settings import AppSettingsRepository
from services.payment import process_successful_payment


logger = logging.getLogger(__name__)


async def run_card_autoconfirm(session: AsyncSession, bot: Bot | None = None) -> dict:
    """Public entry point used by apps/worker/main.py scheduler."""
    repo = AppSettingsRepository(session)
    cfg = await repo.get_card_autoconfirm_settings()
    if not cfg.enabled or cfg.delay_minutes <= 0:
        return {"checked": 0, "confirmed": 0, "disabled": True}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg.delay_minutes)
    exception_ids = set(cfg.exception_telegram_ids or [])

    rows = await session.execute(
        select(Payment)
        .options(selectinload(Payment.user))
        .where(
            Payment.provider == "card_to_card",
            Payment.payment_status == "pending_approval",
            Payment.created_at <= cutoff,
        )
        .with_for_update(skip_locked=True)
    )
    pending: list[Payment] = list(rows.scalars().all())
    if not pending:
        return {"checked": 0, "confirmed": 0}

    confirmed = 0
    skipped_exempt = 0
    failed = 0

    for payment in pending:
        u: User | None = payment.user
        if u is not None and u.telegram_id in exception_ids:
            skipped_exempt += 1
            logger.info(
                "[CARD-AUTOCONFIRM] payment=%s user_telegram_id=%s on exception list — skipping",
                payment.id, u.telegram_id,
            )
            continue

        try:
            async with session.begin_nested():
                # Stamp the auto-confirm marker BEFORE credit so a re-run
                # never double-credits even if process_successful_payment's
                # own idempotency check somehow fails.
                payload = dict(payment.callback_payload or {})
                payload["card_autoconfirm_at"] = datetime.now(timezone.utc).isoformat()
                payload["card_autoconfirm_delay_minutes"] = cfg.delay_minutes
                payment.callback_payload = payload
                await session.flush()

                await process_successful_payment(
                    session=session,
                    payment=payment,
                    amount_to_credit=Decimal(str(payment.price_amount)),
                )
        except Exception as exc:
            failed += 1
            logger.error(
                "[CARD-AUTOCONFIRM] process_successful_payment failed for payment=%s: %s",
                payment.id, exc, exc_info=True,
            )
            continue

        confirmed += 1
        logger.info(
            "[CARD-AUTOCONFIRM] confirmed payment=%s user_telegram_id=%s amount=%s USD",
            payment.id, (u.telegram_id if u else None), payment.price_amount,
        )

        # Best-effort: tell the buyer the receipt was approved.
        if bot is not None and u is not None:
            try:
                await bot.send_message(
                    u.telegram_id,
                    "✅ <b>رسید پرداختی شما به‌صورت خودکار تأیید شد</b>\n"
                    f"💰 کیف پول شما <b>{payment.price_amount:.2f} $</b> شارژ شد.",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.warning("card autoconfirm notify failed: %s", exc)

        # Best-effort: sales-report channel.
        if bot is not None and u is not None:
            try:
                from services.sales_notifications import notify_wallet_topup as _notify
                # Re-fetch user with wallet eager-loaded so the sales-notification
                # helper can render the post-credit balance correctly.
                from models.user import User as _U
                from sqlalchemy.orm import selectinload as _sel
                u_full = await session.scalar(
                    select(_U).options(_sel(_U.wallet)).where(_U.id == payment.user_id)
                )
                if u_full:
                    await _notify(
                        session, bot,
                        user=u_full,
                        amount_usd=payment.price_amount,
                        payment_method="card_autoconfirm",
                    )
            except Exception as exc:
                logger.warning("card autoconfirm sales-notify failed: %s", exc)

    if confirmed or failed or skipped_exempt:
        logger.info(
            "[CARD-AUTOCONFIRM] sweep — checked=%d confirmed=%d skipped_exempt=%d failed=%d delay=%dm",
            len(pending), confirmed, skipped_exempt, failed, cfg.delay_minutes,
        )

    return {
        "checked": len(pending),
        "confirmed": confirmed,
        "skipped_exempt": skipped_exempt,
        "failed": failed,
        "delay_minutes": cfg.delay_minutes,
    }
