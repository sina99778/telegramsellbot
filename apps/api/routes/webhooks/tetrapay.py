from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.db import get_db_session
from core.config import settings
from models.payment import Payment
from repositories.settings import AppSettingsRepository
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

    if payment.provider != "tetrapay":
        logger.warning("TetraPay IPN: Payment %s is provider=%s", payment.id, payment.provider)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")

    if payment.provider_payment_id and payment.provider_payment_id != payload.authority:
        logger.warning(
            "TetraPay IPN: authority mismatch for payment %s (db=%s, payload=%s)",
            payment.id,
            payment.provider_payment_id,
            payload.authority,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid payment authority")

    # Idempotency guard
    if (
        payment.actually_paid is not None
        and (
            (payment.kind == "direct_purchase" and (payment.callback_payload or {}).get("provisioned"))
            or (payment.kind == "direct_renewal" and (payment.callback_payload or {}).get("renewal_applied"))
            or payment.kind not in {"direct_purchase", "direct_renewal"}
        )
    ):
        logger.info("TetraPay IPN: Payment %s already processed", payment.id)
        return {"status": "already_processed"}

    # 2. Verify payment with TetraPay before mutating local payment state.
    try:
        gw = await AppSettingsRepository(session).get_gateway_settings()
        api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=api_key,
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

    _ensure_tetrapay_verification_matches_payment(
        payment=payment,
        payload=payload,
        verify_res=verify_res,
    )

    if str(payload.status) != "100" or str(verify_res.status) != "100":
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


def _ensure_tetrapay_verification_matches_payment(
    *,
    payment: Payment,
    payload: TetraPayCallbackPayload,
    verify_res,
) -> None:
    verified_hash_id = str(verify_res.Hash_id or "").strip()
    if verified_hash_id != payment.order_id or verified_hash_id != payload.hash_id:
        logger.warning(
            "TetraPay IPN: verified Hash_id mismatch for payment %s (db=%s, payload=%s, verified=%s)",
            payment.id,
            payment.order_id,
            payload.hash_id,
            verified_hash_id,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    verified_authority = str(verify_res.authority or "").strip()
    if verified_authority and verified_authority != payload.authority:
        logger.warning(
            "TetraPay IPN: verified authority mismatch for payment %s (payload=%s, verified=%s)",
            payment.id,
            payload.authority,
            verified_authority,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    if not verified_authority:
        logger.warning("TetraPay IPN: verification authority missing for payment %s", payment.id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")
