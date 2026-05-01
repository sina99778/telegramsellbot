from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies.db import get_db_session
from core.config import settings
from models.payment import Payment
from repositories.settings import AppSettingsRepository
from schemas.internal.tronado import TronadoCallbackPayload, TronadoStatusResponse
from services.payment import process_successful_payment
from services.tronado.client import TronadoClient, TronadoClientConfig, TronadoRequestError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tronado", tags=["webhooks", "tronado"])
_TRON_AMOUNT_TOLERANCE = Decimal("0.000001")


@router.post("")
async def tronado_webhook_handler(
    payload: TronadoCallbackPayload,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    payment_id = payload.payment_id.strip()
    if not payment_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing PaymentID")

    payment = await session.scalar(
        select(Payment).where(Payment.order_id == payment_id).with_for_update()
    )
    if payment is None or payment.provider != "tronado":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")

    status_response = await _fetch_tronado_status(session, payment_id)
    _ensure_tronado_status_matches_payment(payment=payment, status_response=status_response)

    callback_payload = dict(payment.callback_payload or {})
    callback_payload["tronado_callback"] = payload.model_dump(mode="json", by_alias=True)
    callback_payload["tronado_status"] = status_response.model_dump(mode="json")
    payment.callback_payload = callback_payload

    if not status_response.IsPaid:
        payment.payment_status = str(status_response.OrderStatusTitle or "not_paid").lower()
        await session.commit()
        return {"status": "not_paid"}

    if status_response.Hash:
        payment.provider_payment_id = status_response.Hash
    payment.payment_status = "finished"

    if (
        payment.actually_paid is not None
        and (
            (payment.kind == "direct_purchase" and callback_payload.get("provisioned"))
            or (payment.kind == "direct_renewal" and callback_payload.get("renewal_applied"))
            or payment.kind not in {"direct_purchase", "direct_renewal"}
        )
    ):
        return {"status": "already_processed"}

    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=payment.price_amount,
        )
    except Exception as exc:
        logger.error("Tronado callback failed to process payment %s: %s", payment.id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {exc}",
        ) from exc

    return {"status": "processed"}


async def _fetch_tronado_status(session: AsyncSession, payment_id: str) -> TronadoStatusResponse:
    gw = await AppSettingsRepository(session).get_gateway_settings()
    api_key = gw.tronado_api_key or settings.tronado_api_key.get_secret_value()
    try:
        async with TronadoClient(
            TronadoClientConfig(api_key=api_key, base_url=settings.tronado_base_url)
        ) as client:
            return await client.get_status_by_payment_id(payment_id)
    except TronadoRequestError as exc:
        logger.error("Tronado status lookup failed for %s: %s", payment_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to verify payment with Tronado",
        ) from exc


def _ensure_tronado_status_matches_payment(
    *,
    payment: Payment,
    status_response: TronadoStatusResponse,
) -> None:
    if status_response.Error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=status_response.Error)

    verified_payment_id = str(status_response.PaymentID or "").strip()
    if verified_payment_id != payment.order_id:
        logger.warning(
            "Tronado status PaymentID mismatch for payment %s (db=%s, verified=%s)",
            payment.id,
            payment.order_id,
            verified_payment_id,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    verified_wallet = str(status_response.Wallet or "").strip()
    if payment.pay_address and verified_wallet and verified_wallet != payment.pay_address:
        logger.warning(
            "Tronado status wallet mismatch for payment %s (db=%s, verified=%s)",
            payment.id,
            payment.pay_address,
            verified_wallet,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment")

    expected_tron_amount = Decimal(str(payment.pay_amount or "0"))
    paid_tron_amount = status_response.ActualTronAmount or status_response.TronAmount
    if expected_tron_amount > 0 and paid_tron_amount is None:
        logger.warning("Tronado status amount missing for payment %s (expected=%s)", payment.id, expected_tron_amount)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment amount")
    if expected_tron_amount > 0 and paid_tron_amount is not None:
        if paid_tron_amount + _TRON_AMOUNT_TOLERANCE < expected_tron_amount:
            logger.warning(
                "Tronado status amount mismatch for payment %s (expected=%s, paid=%s)",
                payment.id,
                expected_tron_amount,
                paid_tron_amount,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verified payment amount")
