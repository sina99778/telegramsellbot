from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from apps.api.dependencies.db import get_db_session
from models.payment import Payment
from schemas.internal.tetrapay import TetraPayCallbackPayload
from services.payment import process_successful_payment
from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tetrapay", tags=["webhooks", "tetrapay"])


@router.post("")
async def tetrapay_webhook_handler(
    payload: TetraPayCallbackPayload,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Handle incoming TetraPay IPN / Callback requests."""
    
    logger.info("TetraPay IPN received: Hash_id=%s, authority=%s, status=%s", payload.hash_id, payload.authority, payload.status)

    # 1. Find the payment in DB by order_id (Hash_id in tetrapay context)
    if not payload.hash_id:
        # If payload doesn't have it, we could try authority. However TetraPay docs show hash_id in callback.
        logger.warning("TetraPay IPN: missing hash_id")
        return {"status": "ignored"}

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == payload.hash_id).with_for_update()
    )

    if not payment:
        logger.warning("TetraPay IPN: Payment with order_id %s not found", payload.hash_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    # Idempotency guard
    if payment.actually_paid is not None:
        logger.info("TetraPay IPN: Payment %s already processed", payment.id)
        return {"status": "already_processed"}

    if str(payload.status) != "100":
        logger.info("TetraPay IPN: Status is '%s' (not success). Ignoring payment.", payload.status)
        payment.payment_status = "failed"
        await session.commit()
        return {"status": "failed_recorded"}

    # 2. Verify payment with TetraPay
    try:
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=settings.tetrapay_api_key.get_secret_value(),
                base_url=settings.tetrapay_base_url,
            )
        ) as client:
            verify_res = await client.verify_payment(payload.authority)
    except TetraPayRequestError as exc:
        logger.error("TetraPay IPN: Could not verify authority %s: %s", payload.authority, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify payment with provider",
        )

    if str(verify_res.status) != "100":
        logger.error("TetraPay IPN: Verification for %s returned status %s", payload.hash_id, verify_res.status)
        payment.payment_status = "failed"
        await session.commit()
        return {"status": "verification_failed"}

    # 3. Process the payment
    # TetraPay pays in Tomans, we use pay_amount as the credited amount.
    amount_to_credit = payment.price_amount # The service expects amount_to_credit to be in USD (price_currency)

    payment.payment_status = "finished"
    payment.provider_payment_id = payload.authority # Save authority

    logger.info("TetraPay IPN: Verification successful. Processing payment %s", payment.id)

    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=amount_to_credit, # Credit the equivalent USD to the user's wallet (or direct purchase logic)
        )
        logger.info("TetraPay IPN: Payment %s processed SUCCESSFULLY", payment.id)
    except Exception as exc:
        logger.error("TetraPay IPN: FAILED to process payment %s: %s", payment.id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {exc}",
        ) from exc

    return {"status": "processed"}
