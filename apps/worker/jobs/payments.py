from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from core.config import settings
from core.database import AsyncSessionFactory
from models.payment import Payment
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig
from services.wallet.manager import WalletManager


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
                if not payment.provider_payment_id:
                    continue

                status = await client.get_payment_status(payment.provider_payment_id)
                payment.payment_status = status.payment_status
                payment.callback_payload = status.model_dump(mode="json")

                if status.payment_status == "finished" and payment.kind == "wallet_topup" and payment.actually_paid is None:
                    paid_amount = status.actually_paid or status.price_amount
                    payment.actually_paid = paid_amount
                    wallet_manager = WalletManager(session)
                    await wallet_manager.process_transaction(
                        user_id=payment.user_id,
                        amount=Decimal(str(paid_amount)),
                        transaction_type="deposit",
                        direction="credit",
                        currency=payment.price_currency,
                        reference_type="payment",
                        reference_id=payment.id,
                        description="NOWPayments confirmed top-up",
                        metadata={"provider_payment_id": payment.provider_payment_id},
                    )

        await session.commit()
