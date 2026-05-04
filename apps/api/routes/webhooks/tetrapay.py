from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Handle incoming TetraPay IPN / Callback requests.

    TetraPay may send callback data as JSON body or as form data.
    We accept both and also handle field name variations (hash_id, hashid, Hash_id).
    """

    # Parse payload — accept both JSON and form data
    try:
        raw = await request.json()
    except Exception:
        # Fallback: try form data
        form = await request.form()
        raw = dict(form)

    logger.info("TetraPay IPN raw payload: %s", raw)

    # Normalize field names — TetraPay sends inconsistent casing
    normalized: dict[str, str | int] = {}
    for key, val in raw.items():
        normalized[key.lower()] = val

    # Build payload with proper field resolution
    payload_status = normalized.get("status", "")
    payload_authority = str(normalized.get("authority", ""))
    # hash_id can come as: hash_id, hashid, Hash_id
    payload_hash_id = str(
        normalized.get("hash_id")
        or normalized.get("hashid")
        or ""
    )

    logger.info(
        "TetraPay IPN parsed: hash_id=%s, authority=%s, status=%s",
        payload_hash_id, payload_authority, payload_status,
    )

    # 1. Find the payment in DB by order_id (hash_id in tetrapay context)
    if not payload_hash_id:
        logger.warning("TetraPay IPN: missing hash_id in payload")
        return {"status": "ignored"}

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == payload_hash_id).with_for_update()
    )

    if not payment:
        # Fallback: try finding by authority (provider_payment_id)
        if payload_authority:
            payment = await session.scalar(
                select(Payment).where(
                    Payment.provider_payment_id == payload_authority,
                    Payment.provider == "tetrapay",
                ).with_for_update()
            )

    if not payment:
        logger.warning("TetraPay IPN: Payment not found (hash_id=%s, authority=%s)", payload_hash_id, payload_authority)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found",
        )

    if payment.provider != "tetrapay":
        logger.warning("TetraPay IPN: Payment %s is provider=%s", payment.id, payment.provider)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")

    if payment.provider_payment_id and payload_authority and payment.provider_payment_id != payload_authority:
        logger.warning(
            "TetraPay IPN: authority mismatch for payment %s (db=%s, payload=%s)",
            payment.id,
            payment.provider_payment_id,
            payload_authority,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid payment authority")

    # Save the raw IPN payload (preserve existing callback_payload keys like purchase_meta)
    existing_payload = dict(payment.callback_payload or {})
    existing_payload["tetrapay_ipn"] = raw
    payment.callback_payload = existing_payload

    # Idempotency guard — check AFTER preserving existing payload keys
    if (
        payment.actually_paid is not None
        and (
            (payment.kind == "direct_purchase" and existing_payload.get("provisioned"))
            or (payment.kind == "direct_renewal" and existing_payload.get("renewal_applied"))
            or payment.kind not in {"direct_purchase", "direct_renewal"}
        )
    ):
        logger.info("TetraPay IPN: Payment %s already processed", payment.id)
        return {"status": "already_processed"}

    # 2. Verify payment with TetraPay before processing
    if not payload_authority:
        logger.error("TetraPay IPN: missing authority for payment %s", payment.id)
        payment.payment_status = "failed"
        return {"status": "missing_authority"}

    try:
        gw = await AppSettingsRepository(session).get_gateway_settings()
        api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=api_key,
                base_url=settings.tetrapay_base_url,
            )
        ) as client:
            verify_res = await client.verify_payment(payload_authority)
    except TetraPayRequestError as exc:
        logger.error("TetraPay IPN: Could not verify authority %s: %s", payload_authority, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify payment with provider",
        )

    # Validate verification response
    verified_hash_id = str(verify_res.Hash_id or "").strip()
    if verified_hash_id and verified_hash_id != payment.order_id:
        logger.warning(
            "TetraPay IPN: verified Hash_id mismatch (db=%s, verified=%s)",
            payment.order_id, verified_hash_id,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    verified_authority = str(verify_res.authority or "").strip()
    if verified_authority and verified_authority != payload_authority:
        logger.warning(
            "TetraPay IPN: verified authority mismatch (payload=%s, verified=%s)",
            payload_authority, verified_authority,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    # Save verification result in callback_payload
    existing_payload["tetrapay_verify"] = verify_res.model_dump(mode="json")
    payment.callback_payload = existing_payload

    if str(payload_status) != "100" or str(verify_res.status) != "100":
        logger.error(
            "TetraPay IPN: Verification for %s returned status %s (callback_status=%s)",
            payload_hash_id, verify_res.status, payload_status,
        )
        payment.payment_status = "failed"
        return {"status": "verification_failed"}

    # 3. Process the payment
    payment.payment_status = "finished"
    payment.provider_payment_id = payload_authority

    logger.info("TetraPay IPN: Verification successful. Processing payment %s", payment.id)

    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=payment.price_amount,
        )
        logger.info("TetraPay IPN: Payment %s processed SUCCESSFULLY", payment.id)
    except Exception as exc:
        logger.error("TetraPay IPN: FAILED to process payment %s: %s", payment.id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {exc}",
        ) from exc

    return {"status": "processed"}
