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
from sqlalchemy import select, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.payment import Payment
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

MAX_AUTO_RETRY = 5  # Max payments to auto-retry per reconciliation run
MAX_RETRY_COUNT = 10  # Max total retries before giving up on a payment
# Never AUTO-RETRY a payment older than this. Retrying an ancient
# stuck payment calls process_successful_payment, which on a provisioning
# failure REFUNDS the wallet and DMs the user "خرید ناموفق، پول برگشت".
# For payments from days/weeks ago that's harmful spam — and many old
# "stuck" rows are actually already-delivered configs that simply predate
# the `provisioned` callback flag. So anything older than this is escalated
# to manual_review SILENTLY (no retry, no refund, no user message) and the
# admin decides in the Recovery menu.
RETRY_MAX_AGE = timedelta(hours=48)


async def run_reconciliation(session: AsyncSession, bot: Bot) -> None:
    """Find stuck payments, auto-retry provisioning/renewal, and alert admin."""
    now = datetime.now(timezone.utc)

    # ─── HOUSEKEEPING: expire abandoned unpaid invoices ───────────────
    # Invoices stuck in waiting/confirming with nothing ever paid, older
    # than RETRY_MAX_AGE, will NEVER complete (the user walked away). Mark
    # them `expired` so they stop cluttering the Recovery view + counts.
    # Bulk UPDATE — no object load, no user notification, no refund.
    cleaned_abandoned = 0
    try:
        result = await session.execute(
            update(Payment)
            .where(
                Payment.payment_status.in_(["waiting", "confirming"]),
                Payment.actually_paid.is_(None),
                Payment.created_at < (now - RETRY_MAX_AGE),
            )
            .values(payment_status="expired")
        )
        cleaned_abandoned = result.rowcount or 0
        if cleaned_abandoned:
            await session.flush()
            logger.info("[RECONCILIATION] expired %d abandoned unpaid invoices", cleaned_abandoned)
    except Exception as exc:
        logger.warning("[RECONCILIATION] abandoned-invoice cleanup failed: %s", exc)

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
        # Honor process_successful_payment's lock contract; skip rows a
        # concurrent IPN is already processing so we never double-act.
        .with_for_update(skip_locked=True)
    )
    stuck_purchases = list(stuck_purchase_result.scalars().all())

    retried_purchase = 0
    escalated_purchase: list[Payment] = []
    for payment in stuck_purchases:
        retry_count = (payment.callback_payload or {}).get("retry_count", 0)
        created = payment.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        too_old = created is not None and created < (now - RETRY_MAX_AGE)
        if retry_count >= MAX_RETRY_COUNT or too_old:
            # Stop silently retrying — flip to manual_review so an admin sees
            # this payment in Recovery and the worker stops burning cycles.
            # Crucially: NO process_successful_payment here, so NO refund +
            # NO "خرید ناموفق" DM to the user for ancient payments.
            payload = dict(payment.callback_payload or {})
            if not payload.get("escalated"):
                payload["escalated"] = True
                payload["escalated_reason"] = "too_old" if too_old else "max_retries"
                payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
                payment.callback_payload = payload
                payment.payment_status = "manual_review"
                await session.flush()
                escalated_purchase.append(payment)
                logger.error(
                    "[RECONCILIATION] Payment %s escalated to manual_review (%s)",
                    payment.id, "too_old" if too_old else f"after {retry_count} retries",
                )
            continue
        logger.info("[RECONCILIATION] Auto-retrying provisioning for payment %s (attempt %d)", payment.id, retry_count + 1)
        try:
            await process_successful_payment(
                session=session,
                payment=payment,
                amount_to_credit=payment.price_amount,
            )
        except Exception as exc:
            # Increment retry count
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payload["last_error"] = str(exc)[:500]
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Provisioning retry FAILED for payment %s: %s", payment.id, exc)
            continue

        # "Did not raise" is NOT success: process_successful_payment swallows
        # provisioning failures internally (services/payment.py: "Don't
        # re-raise" / silent return on provisioned=False). The only reliable
        # success signal is the `provisioned` flag it writes on real success —
        # otherwise count this as a failed attempt so retry_count still grows
        # and the MAX_RETRY_COUNT escalation stays reachable.
        if (payment.callback_payload or {}).get("provisioned"):
            retried_purchase += 1
            logger.info("[RECONCILIATION] Provisioning retry SUCCESS for payment %s", payment.id)
        else:
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payload["last_error"] = "retry finished without provisioning (failure swallowed upstream)"
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Provisioning retry FAILED (still unprovisioned) for payment %s", payment.id)

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
        # Honor process_successful_payment's lock contract; skip rows a
        # concurrent IPN is already processing so the renewal wallet-debit
        # can't run twice for the same payment.
        .with_for_update(skip_locked=True)
    )
    stuck_renewals = list(stuck_renewal_result.scalars().all())

    retried_renewal = 0
    escalated_renewal: list[Payment] = []
    for payment in stuck_renewals:
        retry_count = (payment.callback_payload or {}).get("retry_count", 0)
        created = payment.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        too_old = created is not None and created < (now - RETRY_MAX_AGE)
        if retry_count >= MAX_RETRY_COUNT or too_old:
            payload = dict(payment.callback_payload or {})
            if not payload.get("escalated"):
                payload["escalated"] = True
                payload["escalated_reason"] = "too_old" if too_old else "max_retries"
                payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
                payment.callback_payload = payload
                payment.payment_status = "manual_review"
                await session.flush()
                escalated_renewal.append(payment)
                logger.error(
                    "[RECONCILIATION] Renewal %s escalated to manual_review (%s)",
                    payment.id, "too_old" if too_old else f"after {retry_count} retries",
                )
            continue
        logger.info("[RECONCILIATION] Auto-retrying renewal for payment %s (attempt %d)", payment.id, retry_count + 1)
        try:
            await process_successful_payment(
                session=session,
                payment=payment,
                amount_to_credit=payment.price_amount,
            )
        except Exception as exc:
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payload["last_error"] = str(exc)[:500]
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Renewal retry FAILED for payment %s: %s", payment.id, exc)
            continue

        # Same as the purchase loop: a swallowed renewal failure (debit →
        # apply_renewal failed → refund → returns without raising) must not
        # count as success. Only the `renewal_applied` flag written on real
        # success does; otherwise grow retry_count so the payment eventually
        # escalates to manual_review instead of re-debiting + DM-ing the user
        # a failure message every cycle for 48h.
        if (payment.callback_payload or {}).get("renewal_applied"):
            retried_renewal += 1
            logger.info("[RECONCILIATION] Renewal retry SUCCESS for payment %s", payment.id)
        else:
            payload = dict(payment.callback_payload or {})
            payload["retry_count"] = retry_count + 1
            payload["last_error"] = (
                "renewal refused (terminal sub status)"
                if payload.get("renewal_refused")
                else "retry finished without applying renewal (failure swallowed upstream)"
            )
            payment.callback_payload = payload
            await session.flush()
            logger.error("[RECONCILIATION] Renewal retry FAILED (still unapplied) for payment %s", payment.id)

    # ─── Decide whether to ALERT ───
    # Only message the operator when the worker actually DID something this
    # run (retried or newly escalated a payment). Standing counts like
    # "208 invoices abandoned >24h" or "23 paid-but-undelivered" never
    # change on their own, so reporting them every hour is pure spam — the
    # operator can see them anytime in the Recovery menu. This is the noise
    # the operator complained about (esp. with sales closed for days).
    retried_total = retried_purchase + retried_renewal
    escalations_now = len(escalated_purchase) + len(escalated_renewal)

    if retried_total == 0 and escalations_now == 0:
        logger.info("Reconciliation: nothing actionable this run — staying silent")
        return

    lines = ["🔔 گزارش Reconciliation خودکار\n"]
    if retried_total > 0:
        lines.append(f"🔄 Retry موفق: {retried_purchase} خرید + {retried_renewal} تمدید")
    if escalations_now > 0:
        # Note WHY they were escalated so the operator knows old payments
        # were parked for manual review (NOT auto-refunded).
        too_old_n = sum(
            1 for p in (escalated_purchase + escalated_renewal)
            if (p.callback_payload or {}).get("escalated_reason") == "too_old"
        )
        lines.append(f"🚨 منتقل‌شده به بررسی دستی: {escalations_now}")
        if too_old_n:
            lines.append(f"   ({too_old_n} مورد قدیمی‌تر از ۴۸ ساعت — بدون رفاند، فقط برای بررسی)")
    lines.append("\nاز منوی 🔧 Recovery اقدام کنید.")
    alert_text = "\n".join(lines)

    logger.warning("Reconciliation alert: retried=%d, escalated=%d", retried_total, escalations_now)

    if settings.owner_telegram_id:
        try:
            await bot.send_message(settings.owner_telegram_id, alert_text)
        except Exception as exc:
            logger.error("Failed to send reconciliation alert: %s", exc)
