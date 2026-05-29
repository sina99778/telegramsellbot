from __future__ import annotations

import logging

from sqlalchemy import select

from core.database import AsyncSessionFactory
from models.payment import Payment
from services.payment import review_gateway_payment

logger = logging.getLogger(__name__)


async def sync_pending_payments() -> None:
    # First, collect candidate payment IDs WITHOUT a lock — just to know what to
    # look at this tick.
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Payment.id).where(
                Payment.payment_status.in_(
                    ["waiting", "confirming", "partially_paid", "sending"]
                ),
                Payment.provider.in_(["nowpayments", "tetrapay", "tronado"]),
            )
        )
        payment_ids = [row[0] for row in result.all()]

    # Process each one in its OWN locked transaction. `process_successful_payment`
    # (reached via review_gateway_payment) documents a hard contract: the caller
    # MUST hold a row lock so it can't race the provider IPN webhook and
    # double-credit the wallet. We honor it here with `with_for_update(
    # skip_locked=True)` — if an IPN is already processing this row, we skip it
    # and let the IPN finish. We lock+commit per row (not per batch) so the lock
    # is held only across that one payment's provider HTTP call, never blocking
    # IPNs for unrelated payments.
    for pid in payment_ids:
        async with AsyncSessionFactory() as session:
            payment = await session.scalar(
                select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
            )
            if payment is None:
                # Locked by a concurrent IPN, or no longer exists — skip.
                continue
            if payment.payment_status not in {"waiting", "confirming", "partially_paid", "sending"}:
                # Status changed between listing and locking (e.g. the IPN
                # finished it). Nothing to do.
                continue
            try:
                result_text = await review_gateway_payment(session, payment)
                logger.info("Payment sync review %s -> %s", payment.id, result_text)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Unexpected error syncing payment %s: %s", pid, exc, exc_info=True)

    # Cleanup stale manual crypto payments (waiting_hash > 48h) — bulk, no lock
    # contention, its own transaction.
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    async with AsyncSessionFactory() as session:
        stale_result = await session.execute(
            select(Payment).where(
                Payment.payment_status == "waiting_hash",
                Payment.created_at < cutoff,
            )
        )
        stale_payments = list(stale_result.scalars().all())
        for sp in stale_payments:
            sp.payment_status = "expired"
            logger.info("Expired stale waiting_hash payment %s (created %s)", sp.id, sp.created_at)
        await session.commit()
