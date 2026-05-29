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

    # Validate signature — REJECT invalid/unsigned callbacks to prevent forgery.
    #
    # FAIL CLOSED when no secret is configured: this endpoint finalizes a
    # payment and credits a wallet, so processing it unauthenticated would let
    # anyone complete a pending invoice. NowPayments only sends IPNs when an
    # IPN secret is set in its dashboard, so a correctly-configured deployment
    # always has one. If you hit this, set NOWPAYMENTS_IPN_SECRET (env) or the
    # gateway override in the panel to the value from your NowPayments account.
    if effective_ipn_secret is None:
        logger.error(
            "NOWPAYMENTS_IPN_SECRET not configured — REJECTING IPN (fail-closed). "
            "Set it to the secret from your NowPayments dashboard to enable callbacks."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IPN secret not configured.",
        )
    if not _is_valid_nowpayments_signature(raw_body=raw_body, signature=signature, ipn_secret=effective_ipn_secret):
        logger.warning(
            "IPN signature validation FAILED — REJECTING request. "
            "Please check NOWPAYMENTS_IPN_SECRET matches your NowPayments dashboard setting!"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid IPN signature.",
        )
    logger.info("IPN signature validated OK")

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

    # SECURITY: credit the amount we INVOICED (from our DB), never the amount
    # echoed back in the IPN body. NowPayments reports "finished" only once the
    # invoice is fully paid, so payment.price_amount is the correct, tamper-proof
    # figure to credit — matching the Tronado/TetraPay handlers. Trusting the
    # payload's `price_amount` would let a forged/replayed body inflate a credit.
    amount_to_credit = payment.price_amount
    if amount_to_credit is None or amount_to_credit <= Decimal("0"):
        logger.error(
            "IPN: payment %s has an invalid invoiced amount (%s) — refusing to credit",
            payment.id, amount_to_credit,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid invoiced amount on payment record.",
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
        # We deliberately return 200 OK here. The payment record is already
        # persisted and the reconciliation worker will retry provisioning.
        # Returning 500 would make NowPayments retry the IPN endlessly while
        # our DB and the provider drift apart.
        logger.error("IPN: deferred provisioning for payment %s: %s", payment.id, exc, exc_info=True)
        payload_dict = dict(payment.callback_payload or {})
        payload_dict["deferred_error"] = str(exc)[:500]
        payment.callback_payload = payload_dict
        return {"status": "accepted_deferred"}

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

