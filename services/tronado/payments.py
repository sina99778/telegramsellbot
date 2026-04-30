from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.payment import Payment
from models.user import User
from repositories.settings import AppSettingsRepository
from services.tronado.client import TronadoClient, TronadoClientConfig, TronadoRequestError


@dataclass(slots=True)
class TronadoInvoice:
    payment: Payment
    invoice_url: str
    token: str
    toman_amount: int
    tron_amount: Decimal


async def create_tronado_invoice(
    *,
    session: AsyncSession,
    user: User,
    amount_usd: Decimal,
    kind: str,
    description: str,
    callback_payload: dict[str, object],
) -> TronadoInvoice:
    repo = AppSettingsRepository(session)
    gw = await repo.get_gateway_settings()
    if not gw.tronado_enabled:
        raise HTTPException(status_code=400, detail="درگاه ترونادو غیرفعال است.")

    wallet_address = gw.tronado_wallet_address or settings.tronado_wallet_address
    if not wallet_address:
        raise HTTPException(status_code=400, detail="آدرس ولت ترونادو تنظیم نشده است.")

    toman_rate = await repo.get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        raise HTTPException(status_code=400, detail="نرخ تبدیل تومان تنظیم نشده است.")

    toman_amount = int((amount_usd * toman_rate).quantize(Decimal("1")))
    if toman_amount <= 0:
        raise HTTPException(status_code=400, detail="مبلغ پرداخت نامعتبر است.")

    api_key = gw.tronado_api_key or settings.tronado_api_key.get_secret_value()
    if not api_key or api_key == "CHANGE_ME":
        raise HTTPException(status_code=400, detail="API Key ترونادو تنظیم نشده است.")

    local_order_id = str(uuid4())
    wage_percentage = gw.tronado_wage_from_business_percentage
    if wage_percentage is None:
        wage_percentage = settings.tronado_wage_from_business_percentage

    try:
        async with TronadoClient(
            TronadoClientConfig(api_key=api_key, base_url=settings.tronado_base_url)
        ) as client:
            conversion = await client.convert_toman_to_tron(toman=toman_amount, wallet=wallet_address)
            order = await client.create_order(
                payment_id=local_order_id,
                wallet_address=wallet_address,
                tron_amount=conversion.TronAmount,
                callback_url=settings.tronado_callback_url,
                wage_from_business_percentage=wage_percentage,
            )
    except TronadoRequestError as exc:
        raise HTTPException(status_code=502, detail=f"خطا در ساخت فاکتور ترونادو: {exc}") from exc

    assert order.Data is not None
    payment = Payment(
        user_id=user.id,
        provider="tronado",
        kind=kind,
        provider_payment_id=None,
        provider_invoice_id=order.Data.Token,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="TRX",
        pay_address=wallet_address,
        pay_amount=conversion.TronAmount,
        price_currency="USD",
        price_amount=amount_usd,
        invoice_url=order.Data.FullPaymentUrl,
        callback_payload={
            **callback_payload,
            "source_gateway": "tronado",
            "toman_amount": toman_amount,
            "estimated_toman_amount": order.Data.EstimatedTomanAmount,
        },
    )
    session.add(payment)
    await session.flush()
    return TronadoInvoice(
        payment=payment,
        invoice_url=order.Data.FullPaymentUrl,
        token=order.Data.Token,
        toman_amount=toman_amount,
        tron_amount=conversion.TronAmount,
    )
