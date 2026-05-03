from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.db import get_db_session
from core.config import settings
from models.payment import Payment
from services.payment import process_successful_payment


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/nowpayments")
async def handle_nowpayments_ipn(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    raw_body = await request.body()
    signature = request.headers.get("x-nowpayments-sig")

    logger.info("IPN received. Signature present: %s, Body length: %d", bool(signature), len(raw_body))

    # Determine effective IPN secret: DB override takes priority over env
    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    effective_ipn_secret = gw.nowpayments_ipn_secret or (
        settings.nowpayments_ipn_secret.get_secret_value()
        if settings.nowpayments_ipn_secret is not None else None
    )

    # Validate signature — REJECT invalid signatures to prevent forged callbacks
    if effective_ipn_secret is not None:
        if not _is_valid_nowpayments_signature(raw_body=raw_body, signature=signature, ipn_secret=effective_ipn_secret):
            logger.warning(
                "IPN signature validation FAILED — REJECTING request. "
                "Please check NOWPAYMENTS_IPN_SECRET matches your NowPayments dashboard setting!"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid IPN signature.",
            )
        else:
            logger.info("IPN signature validated OK")
    else:
        logger.warning("NOWPAYMENTS_IPN_SECRET not configured — skipping signature check!")

    try:
        payload = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("IPN: Invalid JSON payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    provider_payment_id = str(payload.get("payment_id") or "").strip()
    payment_status_str = str(payload.get("payment_status") or "").strip().lower()
    order_id_from_payload = str(payload.get("order_id") or "").strip()
    invoice_id_from_payload = str(payload.get("invoice_id") or "").strip()

    # Guard against "None" strings from the API
    if order_id_from_payload.lower() in ("", "none", "null"):
        order_id_from_payload = ""
    if invoice_id_from_payload.lower() in ("", "none", "null"):
        invoice_id_from_payload = ""

    logger.info(
        "IPN payload: payment_id=%s, status=%s, order_id=%s, invoice_id=%s",
        provider_payment_id, payment_status_str, order_id_from_payload, invoice_id_from_payload,
    )

    if not provider_payment_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing payment_id in NOWPayments callback.",
        )

    # Find payment: try provider_payment_id, then order_id, then invoice_id
    payment = await session.scalar(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id).with_for_update()
    )
    if payment is None and order_id_from_payload:
        logger.info("Payment not found by provider_payment_id, trying order_id=%s", order_id_from_payload)
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id_from_payload).with_for_update()
        )
    if payment is None and invoice_id_from_payload:
        logger.info("Payment not found by order_id, trying provider_invoice_id=%s", invoice_id_from_payload)
        payment = await session.scalar(
            select(Payment).where(Payment.provider_invoice_id == invoice_id_from_payload).with_for_update()
        )

    if payment is None:
        logger.error(
            "IPN: Payment NOT FOUND for payment_id=%s, order_id=%s, invoice_id=%s",
            provider_payment_id, order_id_from_payload, invoice_id_from_payload,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payment not found.",
        )

    logger.info("IPN: Found payment %s (kind=%s, current_status=%s)", payment.id, payment.kind, payment.payment_status)

    # Store provider_payment_id for future lookups
    if not payment.provider_payment_id:
        payment.provider_payment_id = provider_payment_id

    # Update status and payload
    payment.payment_status = payment_status_str
    if isinstance(payment.callback_payload, dict):
        payment.callback_payload = {**payment.callback_payload, "nowpayments_ipn": payload}
    else:
        payment.callback_payload = {"nowpayments_ipn": payload}

    if payment_status_str not in {"finished", "confirmed"}:
        logger.info("IPN: Status '%s' is not final — ignoring", payment_status_str)
        return {"status": "ignored"}

    # Idempotency guard
    if (
        payment.actually_paid is not None
        and (
            (payment.kind == "direct_purchase" and (payment.callback_payload or {}).get("provisioned"))
            or (payment.kind == "direct_renewal" and (payment.callback_payload or {}).get("renewal_applied"))
            or payment.kind not in {"direct_purchase", "direct_renewal"}
        )
    ):
        logger.info("IPN: Payment %s already processed (actually_paid=%s)", payment.id, payment.actually_paid)
        return {"status": "already_processed"}

    amount_to_credit = _extract_credit_amount(payload)
    if amount_to_credit <= Decimal("0"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payment amount in callback payload.",
        )

    logger.info("IPN: Processing payment %s — amount_to_credit=%s", payment.id, amount_to_credit)

    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=amount_to_credit,
        )
        logger.info("IPN: Payment %s processed SUCCESSFULLY", payment.id)
    except Exception as exc:
        logger.error("IPN: FAILED to process payment %s: %s", payment.id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {exc}",
        ) from exc

    return {"status": "processed"}


def _is_valid_nowpayments_signature(
    *,
    raw_body: bytes,
    signature: str | None,
    ipn_secret: str | None = None,
) -> bool:
    if not signature:
        return False
    if ipn_secret is None:
        if settings.nowpayments_ipn_secret is None:
            return False
        ipn_secret = settings.nowpayments_ipn_secret.get_secret_value()

    try:
        canonical_body = json.dumps(
            json.loads(raw_body.decode("utf-8")),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    expected_signature = hmac.HMAC(
        key=ipn_secret.encode("utf-8"),
        msg=canonical_body,
        digestmod=hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


def _extract_credit_amount(payload: dict[str, Any]) -> Decimal:
    """
    Extract the amount to credit in USD.
    
    IMPORTANT: 'actually_paid' is in crypto currency (e.g. 0.003 BTC),
    NOT in USD! We must use 'price_amount' which is the original USD amount.
    """
    # Use price_amount (USD) first — this is what the user actually owes
    raw_amount = payload.get("price_amount") or payload.get("actually_paid")
    if raw_amount in {None, ""}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing payment amount in callback payload.",
        )

    try:
        return Decimal(str(raw_amount))
    except (InvalidOperation, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payment amount in callback payload.",
        ) from exc

