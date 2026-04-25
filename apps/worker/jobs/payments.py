from __future__ import annotations

import logging

from sqlalchemy import select

from core.database import AsyncSessionFactory
from models.payment import Payment
from services.payment import review_gateway_payment

logger = logging.getLogger(__name__)


async def sync_pending_payments() -> None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Payment).where(
                Payment.payment_status.in_(
                    ["waiting", "confirming", "partially_paid", "sending"]
                )
            )
        )
        payments = list(result.scalars().all())

        for payment in payments:
            if payment.provider not in {"nowpayments", "tetrapay"}:
                continue
            try:
                result_text = await review_gateway_payment(session, payment)
                logger.info("Payment sync review %s -> %s", payment.id, result_text)
            except Exception as exc:
                logger.error("Unexpected error syncing payment %s: %s", payment.id, exc, exc_info=True)

        # Cleanup stale manual crypto payments (waiting_hash > 48h)
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
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
