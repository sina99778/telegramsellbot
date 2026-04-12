from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WalletView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    balance: Decimal
    credit_limit: Decimal
    hold_balance: Decimal


class SubscriptionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    used_bytes: int
    volume_bytes: int
    sub_link: str | None


class MiniAppDashboardResponse(BaseModel):
    user_id: UUID
    telegram_id: int
    wallet: WalletView
    subscriptions: list[SubscriptionView]
