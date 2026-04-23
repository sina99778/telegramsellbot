from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select

from core.config import settings
from core.database import AsyncSessionFactory
from models.payment import Payment
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)


async def sync_pending_payments() -> None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Payment).where(Payment.payment_status.in_(["waiting", "confirming", "partially_paid"]))
        )
        payments = list(result.scalars().all())

        async with NowPaymentsClient(
            NowPaymentsClientConfig(
                api_key=settings.nowpayments_api_key,
                base_url=settings.nowpayments_base_url,
            )
        ) as client:
            for payment in payments:
                try:
                    if payment.provider_payment_id:
                        # We have a payment ID — check status directly
                        status = await client.get_payment_status(payment.provider_payment_id)
                    elif payment.provider_invoice_id:
                        # Direct purchase: we only have invoice_id.
                        # NowPayments API doesn't have a "get payments by invoice" endpoint,
                        # so we rely on IPN callback for these.
                        # But we can try to look up via the invoice endpoint.
                        # For now, skip — IPN should handle it.
                        continue
                    else:
                        continue

                    payment.payment_status = status.payment_status

                    # Store the provider_payment_id if we didn't have it
                    if not payment.provider_payment_id and status.payment_id:
                        payment.provider_payment_id = str(status.payment_id)

                    if isinstance(payment.callback_payload, dict):
                        payment.callback_payload = {**payment.callback_payload, "nowpayments_status": status.model_dump(mode="json")}
                    else:
                        payment.callback_payload = {"nowpayments_status": status.model_dump(mode="json")}

                    if status.payment_status in ("finished", "confirmed") and payment.actually_paid is None:
                        # Use price_amount (USD), NOT actually_paid (crypto amount)
                        paid_amount = status.price_amount or status.actually_paid
                        await process_successful_payment(
                            session=session,
                            payment=payment,
                            amount_to_credit=Decimal(str(paid_amount)),
                        )
                except NowPaymentsRequestError as exc:
                    logger.warning("Failed to sync payment %s: %s", payment.id, exc)
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
