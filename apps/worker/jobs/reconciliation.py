"""
Reconciliation job: finds stuck payments and AUTO-RETRIES provisioning/renewal.

Runs periodically to detect and fix:
- Payments that are paid but not provisioned (direct_purchase)
- Payments that are paid but renewal not applied (direct_renewal)
- Payments that are 'waiting' for more than 24 hours
- Failed payments needing manual review
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.payment import Payment
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

MAX_AUTO_RETRY = 5  # Max payments to auto-retry per reconciliation run
MAX_RETRY_COUNT = 10  # Max total retries before giving up on a payment


async def run_reconciliation(session: AsyncSession, bot: Bot) -> None:
    """Find stuck payments, auto-retry provisioning/renewal, and alert admin."""
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # ─── AUTO-RETRY: paid but not provisioned (direct_purchase) ───
    stuck_purchase_result = await session.execute(
        select(Payment).where(
            Payment.actually_paid.isnot(None),
            Payment.kind == "direct_purchase",
            Payment.payment_status == "finished",
            or_(
                ~Payment.callback_payload.has_key("provisioned"),
                Payment.callback_payload["provisioned"].as_boolean().is_(False),
            ),
        ).order_by(Payment.created_at.asc()).limit(MAX_AUTO_RETRY)
    )
    stuck_purchases = list(stuck_purchase_result.scalars().all())

    retried_purchase = 0
    for payment in stuck_purchases:
        retry_count = (payment.callback_payload or {}).get("retry_count", 0)
        if retry_count >= MAX_RETRY_COUNT:
            logger.warning("[RECONCILIATION] Skipping payment %s — max retries (%d) exceeded", payment.id, retry_count)
            continue
        logger.info("[RECONCILIATION] Auto-retrying provisioning for payment %s (attempt %d)", payment.id, retry_count + 1)
        try:
            await process_successful_payment(
                session=session,
                payment=payment,
                amount_to_credit=payment.price_amount,
            )
            retried_purchase += 1
            logger.info("[RECONCILIATION] Provisioning retry SUCCESS for payment %s", payment.id)
        except Exception as exc:
            # Increment retry count
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Provisioning retry FAILED for payment %s: %s", payment.id, exc)

    # ─── AUTO-RETRY: paid but renewal not applied (direct_renewal) ───
    stuck_renewal_result = await session.execute(
        select(Payment).where(
            Payment.actually_paid.isnot(None),
            Payment.kind == "direct_renewal",
            Payment.payment_status == "finished",
            or_(
                ~Payment.callback_payload.has_key("renewal_applied"),
                Payment.callback_payload["renewal_applied"].as_boolean().is_(False),
            ),
        ).order_by(Payment.created_at.asc()).limit(MAX_AUTO_RETRY)
    )
    stuck_renewals = list(stuck_renewal_result.scalars().all())

    retried_renewal = 0
    for payment in stuck_renewals:
        retry_count = (payment.callback_payload or {}).get("retry_count", 0)
        if retry_count >= MAX_RETRY_COUNT:
            logger.warning("[RECONCILIATION] Skipping renewal %s — max retries (%d) exceeded", payment.id, retry_count)
            continue
        logger.info("[RECONCILIATION] Auto-retrying renewal for payment %s (attempt %d)", payment.id, retry_count + 1)
        try:
            await process_successful_payment(
                session=session,
                payment=payment,
                amount_to_credit=payment.price_amount,
            )
            retried_renewal += 1
            logger.info("[RECONCILIATION] Renewal retry SUCCESS for payment %s", payment.id)
        except Exception as exc:
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Renewal retry FAILED for payment %s: %s", payment.id, exc)

    # ─── COUNTS for alerting ───
    # Remaining stuck after retries
    stuck_count = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.actually_paid.isnot(None),
            Payment.kind.in_(["direct_purchase", "direct_renewal"]),
            or_(
                ~Payment.callback_payload.has_key("provisioned"),
                Payment.callback_payload["provisioned"].as_boolean().is_(False),
            ),
            or_(
                ~Payment.callback_payload.has_key("renewal_applied"),
                Payment.callback_payload["renewal_applied"].as_boolean().is_(False),
            ),
        )
    ) or 0

    # Waiting payments older than 24h
    stale_waiting = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["waiting", "confirming"]),
            Payment.actually_paid.is_(None),
            Payment.created_at < cutoff_24h,
        )
    ) or 0

    # Failed payments in last 24h
    recent_failed = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["failed", "expired"]),
            Payment.created_at >= cutoff_24h,
        )
    ) or 0

    # Manual crypto payments pending admin approval for >24h
    stale_manual = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["pending_approval", "waiting_hash"]),
            Payment.created_at < cutoff_24h,
        )
    ) or 0

    # Build alert
    retried_total = retried_purchase + retried_renewal
    if retried_total == 0 and stuck_count == 0 and stale_waiting == 0 and recent_failed == 0 and stale_manual == 0:
        logger.info("Reconciliation: no issues found")
        return

    lines = ["🔔 گزارش Reconciliation خودکار\n"]
    if retried_total > 0:
        lines.append(f"🔄 Retry خودکار: {retried_purchase} provisioning + {retried_renewal} renewal")
    if stuck_count > 0:
        lines.append(f"⚠️ پرداخت موفق بدون تحویل (باقی‌مانده): {stuck_count}")
    if stale_waiting > 0:
        lines.append(f"⏳ پرداخت در انتظار (+24 ساعت): {stale_waiting}")
    if recent_failed > 0:
        lines.append(f"❌ پرداخت ناموفق (24 ساعت اخیر): {recent_failed}")
    if stale_manual > 0:
        lines.append(f"🔐 پرداخت دستی منتظر تأیید (+24 ساعت): {stale_manual}")
    lines.append("\nاز منوی 🔧 Recovery اقدام کنید.")

    alert_text = "\n".join(lines)

    logger.warning(
        "Reconciliation alert: retried=%d, stuck=%d, stale_waiting=%d, recent_failed=%d",
        retried_total, stuck_count, stale_waiting, recent_failed,
    )

    # Send to owner
    if settings.owner_telegram_id:
        try:
            await bot.send_message(settings.owner_telegram_id, alert_text)
        except Exception as exc:
            logger.error("Failed to send reconciliation alert: %s", exc)
