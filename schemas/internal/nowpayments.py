from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


NowPaymentsPaymentStatus = Literal[
    "waiting",
    "confirming",
    "confirmed",
    "sending",
    "partially_paid",
    "finished",
    "failed",
    "expired",
    "wrong_asset_confirmed",
    "cancelled",
]


class NowPaymentsPaymentCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    price_amount: Decimal = Field(..., gt=Decimal("0"))
    price_currency: str = Field(..., min_length=2, max_length=16)
    pay_currency: str | None = Field(default=None, min_length=2, max_length=32)
    ipn_callback_url: HttpUrl | None = None
    order_id: str | None = Field(default=None, max_length=128)
    order_description: str | None = Field(default=None, max_length=512)
    success_url: HttpUrl | None = None
    cancel_url: HttpUrl | None = None
    is_fixed_rate: bool | None = None
    is_fee_paid_by_user: bool | None = None


class NowPaymentsInvoiceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | int
    invoice_url: HttpUrl


class NowPaymentsPaymentStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    payment_id: str | int
    parent_payment_id: str | int | None = None
    invoice_id: str | int | None = None
    payment_status: NowPaymentsPaymentStatus | str
    pay_address: str | None = None
    payin_extra_id: str | None = None
    price_amount: Decimal
    price_currency: str
    pay_amount: Decimal | None = None
    actually_paid: Decimal | None = None
    actually_paid_at_fiat: Decimal | None = None
    pay_currency: str | None = None
    order_id: str | None = None
    order_description: str | None = None
    purchase_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    outcome_amount: Decimal | None = None
    outcome_currency: str | None = None
    payment_extra_ids: dict[str, Any] | list[Any] | None = None
