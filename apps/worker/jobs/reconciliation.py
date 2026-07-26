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

from core.config import settings
from core.database import AsyncSessionFactory
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


async def run_reconciliation(bot: Bot) -> None:
    """Find stuck payments, auto-retry provisioning/renewal, and alert admin.

    Refactored to use per-row sessions (like payments.py::sync_pending_payments)
    so row locks are held only across one payment's work (not across the entire
    function), and a DB error on one row doesn't roll back all other rows' work.
    This fixes:
    - Bug 1: holding FOR UPDATE across slow HTTP/Telegram calls blocks IPNs
    - Bug 2: flush() after an exception throws PendingRollbackError
    """
    now = datetime.now(timezone.utc)

    # ─── HOUSEKEEPING: expire abandoned unpaid invoices ───────────────
    # Invoices stuck in waiting/confirming with nothing ever paid, older
    # than RETRY_MAX_AGE, will NEVER complete (the user walked away). Mark
    # them `expired` so they stop cluttering the Recovery view + counts.
    # Bulk UPDATE — no object load, no user notification, no refund.
    cleaned_abandoned = 0
    async with AsyncSessionFactory() as session:
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
            await session.commit()
            if cleaned_abandoned:
                logger.info("[RECONCILIATION] expired %d abandoned unpaid invoices", cleaned_abandoned)
        except Exception as exc:
            await session.rollback()
            logger.warning("[RECONCILIATION] abandoned-invoice cleanup failed: %s", exc)

    # ─── AUTO-RETRY: paid but not provisioned (direct_purchase) ───
    # Per-row session pattern (mirrors payments.py::sync_pending_payments):
    # first read the candidate IDs with NO lock held, then process each in its
    # own short-lived session that re-locks the single row with
    # skip_locked=True. This keeps a FOR UPDATE lock alive only for the
    # duration of ONE payment's work — never across all rows and never across
    # the slow alert send below (Bug 1) — and isolates each row's transaction
    # so an error on one row can't poison the others with PendingRollbackError
    # (Bug 2).
    retried_purchase = 0
    escalated_reasons: list[str] = []

    async with AsyncSessionFactory() as read_session:
        purchase_ids = list(
            (
                await read_session.execute(
                    select(Payment.id).where(
                        Payment.actually_paid.isnot(None),
                        Payment.kind == "direct_purchase",
                        Payment.payment_status == "finished",
                        or_(
                            ~Payment.callback_payload.has_key("provisioned"),
                            Payment.callback_payload["provisioned"].as_boolean().is_(False),
                        ),
                    ).order_by(Payment.created_at.asc()).limit(MAX_AUTO_RETRY)
                )
            ).scalars().all()
        )

    for pid in purchase_ids:
        async with AsyncSessionFactory() as session:
            try:
                # Re-lock the single row; skip_locked so a concurrent IPN that
                # already holds it is left alone (no double-act).
                payment = await session.scalar(
                    select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
                )
                if payment is None:
                    continue
                retry_count = (payment.callback_payload or {}).get("retry_count", 0)
                created = payment.created_at
                if created and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                too_old = created is not None and created < (now - RETRY_MAX_AGE)
                if retry_count >= MAX_RETRY_COUNT or too_old:
                    # Stop silently retrying — flip to manual_review so an admin
                    # sees this in Recovery and the worker stops burning cycles.
                    # Crucially: NO process_successful_payment here, so NO refund
                    # + NO "خرید ناموفق" DM to the user for ancient payments.
                    payload = dict(payment.callback_payload or {})
                    if not payload.get("escalated"):
                        reason = "too_old" if too_old else "max_retries"
                        payload["escalated"] = True
                        payload["escalated_reason"] = reason
                        payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
                        payment.callback_payload = payload
                        payment.payment_status = "manual_review"
                        await session.commit()
                        escalated_reasons.append(reason)
                        logger.error(
                            "[RECONCILIATION] Payment %s escalated to manual_review (%s)",
                            pid, "too_old" if too_old else f"after {retry_count} retries",
                        )
                    continue
                logger.info("[RECONCILIATION] Auto-retrying provisioning for payment %s (attempt %d)", pid, retry_count + 1)
                try:
                    await process_successful_payment(
                        session=session,
                        payment=payment,
                        amount_to_credit=payment.price_amount,
                    )
                except Exception as exc:
                    # process_successful_payment left the session in a failed
                    # state; roll back before recording the retry so the
                    # bookkeeping write itself doesn't raise PendingRollbackError.
                    await session.rollback()
                    payment = await session.scalar(
                        select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
                    )
                    if payment is not None:
                        payload = dict(payment.callback_payload or {})
                        payload["retry_count"] = retry_count + 1
                        payload["last_error"] = str(exc)[:500]
                        payment.callback_payload = payload
                        await session.commit()
                    logger.error("[RECONCILIATION] Provisioning retry FAILED for payment %s: %s", pid, exc)
                    continue

                # "Did not raise" is NOT success: process_successful_payment
                # swallows provisioning failures internally (services/payment.py:
                # "Don't re-raise" / silent return on provisioned=False). The only
                # reliable success signal is the `provisioned` flag it writes on
                # real success — otherwise count this as a failed attempt so
                # retry_count still grows and MAX_RETRY_COUNT escalation stays
                # reachable.
                if (payment.callback_payload or {}).get("provisioned"):
                    retried_purchase += 1
                    logger.info("[RECONCILIATION] Provisioning retry SUCCESS for payment %s", pid)
                else:
                    payload = dict(payment.callback_payload or {})
                    payload["retry_count"] = retry_count + 1
                    payload["last_error"] = "retry finished without provisioning (failure swallowed upstream)"
                    payment.callback_payload = payload
                    logger.error("[RECONCILIATION] Provisioning retry FAILED (still unprovisioned) for payment %s", pid)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("[RECONCILIATION] purchase-retry row %s failed: %s", pid, exc, exc_info=True)

    # ─── AUTO-RETRY: paid but renewal not applied (direct_renewal) ───
    retried_renewal = 0

    async with AsyncSessionFactory() as read_session:
        renewal_ids = list(
            (
                await read_session.execute(
                    select(Payment.id).where(
                        Payment.actually_paid.isnot(None),
                        Payment.kind == "direct_renewal",
                        Payment.payment_status == "finished",
                        or_(
                            ~Payment.callback_payload.has_key("renewal_applied"),
                            Payment.callback_payload["renewal_applied"].as_boolean().is_(False),
                        ),
                    ).order_by(Payment.created_at.asc()).limit(MAX_AUTO_RETRY)
                )
            ).scalars().all()
        )

    for pid in renewal_ids:
        async with AsyncSessionFactory() as session:
            try:
                payment = await session.scalar(
                    select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
                )
                if payment is None:
                    continue
                retry_count = (payment.callback_payload or {}).get("retry_count", 0)
                created = payment.created_at
                if created and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                too_old = created is not None and created < (now - RETRY_MAX_AGE)
                if retry_count >= MAX_RETRY_COUNT or too_old:
                    payload = dict(payment.callback_payload or {})
                    if not payload.get("escalated"):
                        reason = "too_old" if too_old else "max_retries"
                        payload["escalated"] = True
                        payload["escalated_reason"] = reason
                        payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
                        payment.callback_payload = payload
                        payment.payment_status = "manual_review"
                        await session.commit()
                        escalated_reasons.append(reason)
                        logger.error(
                            "[RECONCILIATION] Renewal %s escalated to manual_review (%s)",
                            pid, "too_old" if too_old else f"after {retry_count} retries",
                        )
                    continue
                logger.info("[RECONCILIATION] Auto-retrying renewal for payment %s (attempt %d)", pid, retry_count + 1)
                try:
                    await process_successful_payment(
                        session=session,
                        payment=payment,
                        amount_to_credit=payment.price_amount,
                    )
                except Exception as exc:
                    await session.rollback()
                    payment = await session.scalar(
                        select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
                    )
                    if payment is not None:
                        payload = dict(payment.callback_payload or {})
                        payload["retry_count"] = retry_count + 1
                        payload["last_error"] = str(exc)[:500]
                        payment.callback_payload = payload
                        await session.commit()
                    logger.error("[RECONCILIATION] Renewal retry FAILED for payment %s: %s", pid, exc)
                    continue

                # Same as the purchase loop: a swallowed renewal failure (debit →
                # apply_renewal failed → refund → returns without raising) must
                # not count as success. Only the `renewal_applied` flag written on
                # real success does; otherwise grow retry_count so the payment
                # eventually escalates to manual_review instead of re-debiting +
                # DM-ing the user a failure message every cycle for 48h.
                if (payment.callback_payload or {}).get("renewal_applied"):
                    retried_renewal += 1
                    logger.info("[RECONCILIATION] Renewal retry SUCCESS for payment %s", pid)
                else:
                    payload = dict(payment.callback_payload or {})
                    payload["retry_count"] = retry_count + 1
                    payload["last_error"] = (
                        "renewal refused (terminal sub status)"
                        if payload.get("renewal_refused")
                        else "retry finished without applying renewal (failure swallowed upstream)"
                    )
                    payment.callback_payload = payload
                    logger.error("[RECONCILIATION] Renewal retry FAILED (still unapplied) for payment %s", pid)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("[RECONCILIATION] renewal-retry row %s failed: %s", pid, exc, exc_info=True)

    # ─── Decide whether to ALERT ───
    # Only message the operator when the worker actually DID something this
    # run (retried or newly escalated a payment). Standing counts like
    # "208 invoices abandoned >24h" or "23 paid-but-undelivered" never
    # change on their own, so reporting them every hour is pure spam — the
    # operator can see them anytime in the Recovery menu. This is the noise
    # the operator complained about (esp. with sales closed for days).
    retried_total = retried_purchase + retried_renewal
    escalations_now = len(escalated_reasons)

    if retried_total == 0 and escalations_now == 0:
        logger.info("Reconciliation: nothing actionable this run — staying silent")
        return

    lines = ["🔔 گزارش Reconciliation خودکار\n"]
    if retried_total > 0:
        lines.append(f"🔄 Retry موفق: {retried_purchase} خرید + {retried_renewal} تمدید")
    if escalations_now > 0:
        # Note WHY they were escalated so the operator knows old payments
        # were parked for manual review (NOT auto-refunded).
        too_old_n = sum(1 for r in escalated_reasons if r == "too_old")
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
