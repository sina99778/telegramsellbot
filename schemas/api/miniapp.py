"""
Extended Mini App schemas for the full-featured dashboard.
"""
from __future__ import annotations

from datetime import datetime
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
    plan_name: str | None = None
    plan_price: Decimal | None = None
    plan_duration_days: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    config_name: str | None = None  # xui_client username


class PlanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    code: str
    name: str
    protocol: str
    duration_days: int
    volume_gb: float
    price: Decimal
    currency: str


class TransactionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    type: str
    direction: str
    amount: Decimal
    currency: str
    balance_before: Decimal
    balance_after: Decimal
    description: str | None
    created_at: datetime


class TicketView(BaseModel):
    id: UUID
    status: str
    created_at: datetime
    messages: list[TicketMessageView]


class TicketMessageView(BaseModel):
    sender_type: str  # "user" or "admin"
    text: str | None
    photo_id: str | None
    created_at: datetime


class ReferralView(BaseModel):
    ref_code: str | None
    referral_count: int
    total_earned: Decimal
    enabled: bool


class MiniAppDashboardResponse(BaseModel):
    user_id: UUID
    telegram_id: int
    first_name: str | None
    username: str | None
    is_admin: bool = False
    wallet: WalletView
    subscriptions: list[SubscriptionView]
    active_config_count: int
    total_volume_used: int
    total_volume: int


class PlanListResponse(BaseModel):
    plans: list[PlanView]


class TransactionListResponse(BaseModel):
    transactions: list[TransactionView]
    total: int


class TicketListResponse(BaseModel):
    tickets: list[TicketView]


class SendTicketRequest(BaseModel):
    text: str


class MiniAppConfigResponse(BaseModel):
    bot_username: str | None
    web_base_url: str


class PurchaseRequest(BaseModel):
    plan_id: UUID
    config_name: str
    payment_method: str


class PurchaseResponse(BaseModel):
    status: str
    message: str
    payment_method: str
    invoice_url: str | None = None
    payment_id: UUID | None = None
    subscription_id: UUID | None = None
    sub_link: str | None = None
    vless_uri: str | None = None


class RenewalQuoteRequest(BaseModel):
    subscription_id: UUID
    renew_type: str
    amount: float


class RenewalQuoteResponse(BaseModel):
    renew_type: str
    amount: float
    price: Decimal
    currency: str = "USD"


class RenewalRequest(RenewalQuoteRequest):
    payment_method: str = "wallet"


class RenewalResponse(BaseModel):
    status: str
    message: str
    price: Decimal
    balance: Decimal | None = None


class AdminModuleView(BaseModel):
    title: str
    description: str
    callback: str


class MiniAppAdminOverviewResponse(BaseModel):
    users_count: int
    customers_count: int
    active_subscriptions_count: int
    open_tickets_count: int
    waiting_payments_count: int
    active_servers_count: int
    active_plans_count: int
    modules: list[AdminModuleView]
