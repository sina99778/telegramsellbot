"""
Full Mini App API endpoints.
Provides dashboard, plans, configs, wallet, tickets, and referral data.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from decimal import Decimal
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.dependencies.db import get_db_session
from core.config import settings
from core.miniapp_auth import verify_miniapp_session_token
from models.plan import Plan
from models.order import Order
from models.payment import Payment
from models.subscription import Subscription
from models.ticket import Ticket, TicketMessage
from models.user import User
from models.wallet import WalletTransaction
from models.xui import XUIClientRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from repositories.ticket import TicketRepository
from schemas.api.miniapp import (
    MiniAppConfigResponse,
    MiniAppDashboardResponse,
    MiniAppAdminOverviewResponse,
    AdminModuleView,
    PlanListResponse,
    PlanView,
    PurchaseRequest,
    PurchaseResponse,
    ReferralView,
    RenewalQuoteRequest,
    RenewalQuoteResponse,
    RenewalRequest,
    RenewalResponse,
    SendTicketRequest,
    SubscriptionView,
    TicketListResponse,
    TicketMessageView,
    TicketView,
    TransactionListResponse,
    TransactionView,
    WalletView,
)
from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.renewal import apply_renewal, calculate_renewal_price
from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError
from services.wallet.manager import InsufficientBalanceError, WalletManager

logger = logging.getLogger(__name__)
router = APIRouter()
CONFIG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{3,32}$")


def _is_admin_user(user: User) -> bool:
    return user.role in {"admin", "owner"} or user.telegram_id == settings.owner_telegram_id


@router.get("/config", response_model=MiniAppConfigResponse)
async def get_miniapp_config() -> MiniAppConfigResponse:
    return MiniAppConfigResponse(
        bot_username=settings.bot_username.lstrip("@") if settings.bot_username else None,
        web_base_url=settings.web_base_url.rstrip("/"),
    )


# ─── Auth helper ─────────────────────────────────────────────────────────────

async def _get_current_user(
    init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    _auth: str | None = None,  # Query param fallback
    _session: str | None = None,  # Bot-signed fallback
    session: AsyncSession = Depends(get_db_session),
) -> tuple[User, AsyncSession]:
    # Try header first, then query param fallback
    auth_data = init_data or _auth
    if not auth_data and not _session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="لطفاً از داخل ربات تلگرام وارد شوید. (initData is empty)",
        )
    telegram_user_id = validate_telegram_init_data(auth_data) if auth_data else verify_miniapp_session_token(_session or "")
    if telegram_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="لطفاً /start را بزنید و پنل کاربری را از دکمه جدید ربات باز کنید.",
        )
    query = (
        select(User)
        .options(
            selectinload(User.wallet),
            selectinload(User.subscriptions).selectinload(Subscription.plan),
            selectinload(User.subscriptions).selectinload(Subscription.xui_client),
        )
        .where(User.telegram_id == telegram_user_id)
    )
    result = await session.execute(query)
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user, session


# ─── Dashboard ───────────────────────────────────────────────────────────────

@router.get("/me", response_model=MiniAppDashboardResponse)
async def get_dashboard(
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> MiniAppDashboardResponse:
    user, session = auth
    if user.wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    subs = []
    total_used = 0
    total_vol = 0
    active_count = 0

    for sub in user.subscriptions:
        plan_name = sub.plan.name if sub.plan else None
        plan_price = sub.plan.price if sub.plan else None
        plan_dur = sub.plan.duration_days if sub.plan else None
        config_name = sub.xui_client.username if sub.xui_client else None

        subs.append(SubscriptionView(
            id=sub.id,
            status=sub.status,
            used_bytes=sub.used_bytes,
            volume_bytes=sub.volume_bytes,
            sub_link=sub.sub_link,
            plan_name=plan_name,
            plan_price=plan_price,
            plan_duration_days=plan_dur,
            starts_at=sub.starts_at,
            ends_at=sub.ends_at,
            config_name=config_name,
        ))
        if sub.status in ("active", "pending_activation"):
            active_count += 1
            total_used += sub.used_bytes
            total_vol += sub.volume_bytes

    return MiniAppDashboardResponse(
        user_id=user.id,
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        username=user.username,
        is_admin=_is_admin_user(user),
        wallet=WalletView.model_validate(user.wallet),
        subscriptions=subs,
        active_config_count=active_count,
        total_volume_used=total_used,
        total_volume=total_vol,
    )


@router.get("/admin/overview", response_model=MiniAppAdminOverviewResponse)
async def get_admin_overview(
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> MiniAppAdminOverviewResponse:
    user, session = auth
    if not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="دسترسی مدیریت ندارید.")

    users_count = await session.scalar(select(func.count()).select_from(User)) or 0
    customers_count = await session.scalar(
        select(func.count(func.distinct(Order.user_id))).where(
            Order.status.in_(["provisioned", "paid", "completed"])
        )
    ) or 0
    active_subscriptions_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.status.in_(["active", "pending_activation"])
        )
    ) or 0
    open_tickets_count = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status.in_(["open", "answered"]))
    ) or 0
    waiting_payments_count = await session.scalar(
        select(func.count()).select_from(Payment).where(Payment.payment_status.in_(["waiting", "pending"]))
    ) or 0
    active_servers_count = await session.scalar(
        select(func.count()).select_from(XUIServerRecord).where(XUIServerRecord.is_active.is_(True))
    ) or 0
    active_plans_count = await session.scalar(
        select(func.count()).select_from(Plan).where(Plan.is_active.is_(True))
    ) or 0

    modules = [
        AdminModuleView(title="آمار و گزارش‌ها", description="وضعیت فروش، کاربران و سرویس‌ها", callback="admin:stats"),
        AdminModuleView(title="مدیریت مالی", description="بازبینی پرداخت‌ها، پرداخت دستی و ریکاوری", callback="admin:finance"),
        AdminModuleView(title="کاربران", description="لیست، جستجو، موجودی، بن و نقش‌ها", callback="admin:users"),
        AdminModuleView(title="مشتریان", description="کاربران خریدار و کانفیگ‌هایشان", callback="admin:customers"),
        AdminModuleView(title="سرویس‌ها", description="اشتراک‌ها، کانفیگ‌ها و وضعیت‌ها", callback="admin:subs"),
        AdminModuleView(title="پلن‌ها", description="ساخت، مشاهده و فعال/غیرفعال کردن پلن‌ها", callback="admin:plans"),
        AdminModuleView(title="سرورها", description="مدیریت سرورهای X-UI و اینباندها", callback="admin:servers"),
        AdminModuleView(title="تیکت‌ها", description="بررسی و پاسخ به پشتیبانی", callback="admin:tickets"),
        AdminModuleView(title="تخفیف‌ها", description="ساخت و مدیریت کدهای تخفیف", callback="admin:discounts"),
        AdminModuleView(title="تنظیمات ربات", description="درگاه‌ها، تست کانفیگ، ریفرال و نرخ‌ها", callback="admin:settings"),
        AdminModuleView(title="پیام همگانی", description="ارسال پیام به کاربران", callback="admin:broadcast"),
        AdminModuleView(title="ریتارگتینگ", description="تنظیم یادآوری کاربران غیرفعال", callback="admin:retargeting"),
        AdminModuleView(title="بکاپ", description="دریافت فایل پشتیبان", callback="admin:backup"),
    ]
    return MiniAppAdminOverviewResponse(
        users_count=int(users_count),
        customers_count=int(customers_count),
        active_subscriptions_count=int(active_subscriptions_count),
        open_tickets_count=int(open_tickets_count),
        waiting_payments_count=int(waiting_payments_count),
        active_servers_count=int(active_servers_count),
        active_plans_count=int(active_plans_count),
        modules=modules,
    )


# ─── Plans ───────────────────────────────────────────────────────────────────

@router.get("/plans", response_model=PlanListResponse)
async def get_plans(
    session: AsyncSession = Depends(get_db_session),
) -> PlanListResponse:
    result = await session.execute(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price.asc())
    )
    plans = list(result.scalars().all())
    return PlanListResponse(
        plans=[
            PlanView(
                id=p.id,
                code=p.code,
                name=p.name,
                protocol=p.protocol,
                duration_days=p.duration_days,
                volume_gb=round(p.volume_bytes / (1024**3), 2),
                price=p.price,
                currency=p.currency,
            )
            for p in plans
        ]
    )


# ─── Purchase ────────────────────────────────────────────────────────────────

@router.post("/purchase", response_model=PurchaseResponse)
async def create_purchase(
    body: PurchaseRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> PurchaseResponse:
    user, session = auth
    config_name = body.config_name.strip()
    payment_method = body.payment_method.strip().lower()

    if not CONFIG_NAME_PATTERN.match(config_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="نام کانفیگ نامعتبر است. فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.",
        )

    duplicate_config = await session.scalar(
        select(XUIClientRecord).where(XUIClientRecord.username == config_name)
    )
    if duplicate_config is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="این نام کانفیگ قبلاً استفاده شده است.",
        )

    plan = await session.get(Plan, body.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="این پلن در دسترس نیست.")

    if user.wallet is None:
        raise HTTPException(status_code=404, detail="کیف پول کاربر پیدا نشد.")

    if payment_method == "wallet":
        return await _purchase_with_wallet(session, user, plan, config_name)
    if payment_method == "nowpayments":
        return await _create_nowpayments_purchase(session, user, plan, config_name)
    if payment_method == "tetrapay":
        return await _create_tetrapay_purchase(session, user, plan, config_name)

    raise HTTPException(status_code=400, detail="روش پرداخت نامعتبر است.")


async def _purchase_with_wallet(
    session: AsyncSession,
    user: User,
    plan: Plan,
    config_name: str,
) -> PurchaseResponse:
    final_price = plan.price
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status="processing",
        source="miniapp",
        amount=final_price,
        currency=plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        await WalletManager(session).process_transaction(
            user_id=user.id,
            amount=Decimal(str(final_price)),
            transaction_type="purchase",
            direction="debit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description=f"Purchase of plan {plan.code}",
            metadata={"plan_id": str(plan.id), "config_name": config_name, "source": "miniapp"},
        )
    except InsufficientBalanceError as exc:
        order.status = "failed"
        raise HTTPException(status_code=402, detail="موجودی کیف پول کافی نیست.") from exc

    try:
        provisioned = await ProvisioningManager(session).provision_subscription(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
            config_name=config_name,
        )
    except ProvisioningError as exc:
        order.status = "refunded"
        await WalletManager(session).process_transaction(
            user_id=user.id,
            amount=Decimal(str(final_price)),
            transaction_type="refund",
            direction="credit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description="Automatic refund after mini app provisioning failure",
            metadata={"plan_id": str(plan.id), "source": "miniapp"},
        )
        raise HTTPException(
            status_code=500,
            detail="ساخت کانفیگ انجام نشد و مبلغ به کیف پول برگشت داده شد.",
        ) from exc

    order.status = "provisioned"
    return PurchaseResponse(
        status="provisioned",
        message="خرید با کیف پول انجام شد و کانفیگ ساخته شد.",
        payment_method="wallet",
        subscription_id=provisioned.subscription.id,
        sub_link=provisioned.sub_link,
        vless_uri=provisioned.vless_uri,
    )


async def _create_nowpayments_purchase(
    session: AsyncSession,
    user: User,
    plan: Plan,
    config_name: str,
) -> PurchaseResponse:
    from repositories.settings import AppSettingsRepository

    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.nowpayments_enabled:
        raise HTTPException(status_code=400, detail="درگاه NOWPayments غیرفعال است.")

    api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key
    local_order_id = str(uuid4())
    payload = NowPaymentsPaymentCreateRequest(
        price_amount=plan.price,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Purchase plan {plan.name} for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )

    try:
        async with NowPaymentsClient(
            NowPaymentsClientConfig(api_key=api_key, base_url=settings.nowpayments_base_url)
        ) as client:
            invoice = await client.create_payment_invoice(payload)
    except NowPaymentsRequestError as exc:
        raise HTTPException(status_code=502, detail="خطا در ساخت فاکتور درگاه ارزی.") from exc

    payment = Payment(
        user_id=user.id,
        provider="nowpayments",
        kind="direct_purchase",
        provider_payment_id=None,
        provider_invoice_id=str(invoice.id),
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency=None,
        price_currency="USD",
        price_amount=plan.price,
        invoice_url=str(invoice.invoice_url),
        callback_payload={
            "plan_id": str(plan.id),
            "config_name": config_name,
            "discount_percent": 0,
            "discount_id": None,
            "purpose": "direct_purchase",
            "source": "miniapp",
        },
    )
    session.add(payment)
    await session.flush()

    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
        payment_method="nowpayments",
        invoice_url=str(invoice.invoice_url),
        payment_id=payment.id,
    )


async def _create_tetrapay_purchase(
    session: AsyncSession,
    user: User,
    plan: Plan,
    config_name: str,
) -> PurchaseResponse:
    from repositories.settings import AppSettingsRepository

    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    if not gw.tetrapay_enabled:
        raise HTTPException(status_code=400, detail="درگاه تتراپی غیرفعال است.")

    toman_rate = await settings_repo.get_toman_rate()
    toman_amount = int((plan.price * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10
    if rial_amount < 10000:
        raise HTTPException(status_code=400, detail="مبلغ این پلن کمتر از حداقل مجاز درگاه تتراپی است.")

    local_order_id = str(uuid4())
    api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()

    try:
        async with TetraPayClient(
            TetraPayClientConfig(api_key=api_key, base_url=settings.tetrapay_base_url)
        ) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"خرید سرویس {plan.name} - کاربر {user.telegram_id}",
                email=f"{user.telegram_id}@telegram.org",
                mobile="09111111111",
            )
    except TetraPayRequestError as exc:
        raise HTTPException(status_code=502, detail="خطا در ساخت فاکتور تتراپی.") from exc

    payment = Payment(
        user_id=user.id,
        provider="tetrapay",
        kind="direct_purchase",
        provider_payment_id=tx.Authority,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=plan.price,
        pay_amount=toman_amount,
        invoice_url=tx.payment_url_bot,
        callback_payload={
            "plan_id": str(plan.id),
            "config_name": config_name,
            "discount_percent": 0,
            "discount_id": None,
            "purpose": "direct_purchase",
            "source": "miniapp",
        },
    )
    session.add(payment)
    await session.flush()

    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
        payment_method="tetrapay",
        invoice_url=tx.payment_url_bot,
        payment_id=payment.id,
    )


# ─── Renewal ─────────────────────────────────────────────────────────────────

@router.post("/renewal/quote", response_model=RenewalQuoteResponse)
async def get_renewal_quote(
    body: RenewalQuoteRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> RenewalQuoteResponse:
    user, session = auth
    subscription = await _get_user_subscription(session, user, body.subscription_id)
    _validate_renewal_request(subscription, body.renew_type, body.amount)
    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()
    price = calculate_renewal_price(
        renew_type=body.renew_type,
        amount=body.amount,
        settings=renewal_settings,
    )
    return RenewalQuoteResponse(
        renew_type=body.renew_type,
        amount=body.amount,
        price=price,
    )


@router.post("/renewal", response_model=RenewalResponse)
async def renew_subscription(
    body: RenewalRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> RenewalResponse:
    user, session = auth
    if body.payment_method != "wallet":
        raise HTTPException(status_code=400, detail="فعلاً تمدید داخل مینی‌اپ با کیف پول انجام می‌شود.")
    if user.wallet is None:
        raise HTTPException(status_code=404, detail="کیف پول پیدا نشد.")

    subscription = await _get_user_subscription(session, user, body.subscription_id)
    _validate_renewal_request(subscription, body.renew_type, body.amount)

    renewal_settings = await AppSettingsRepository(session).get_renewal_settings()
    price = calculate_renewal_price(
        renew_type=body.renew_type,
        amount=body.amount,
        settings=renewal_settings,
    )
    if user.wallet.balance < price:
        raise HTTPException(status_code=402, detail="موجودی کیف پول برای تمدید کافی نیست.")

    order = Order(
        user_id=user.id,
        plan_id=subscription.plan_id,
        amount=price,
        currency="USD",
        status="completed",
        source="miniapp",
    )
    session.add(order)
    await session.flush()
    subscription.order_id = order.id

    await WalletManager(session).process_transaction(
        user_id=user.id,
        amount=price,
        transaction_type="renewal",
        direction="debit",
        currency="USD",
        reference_type="order",
        reference_id=order.id,
        description=f"Renewal of subscription {subscription.id}",
        metadata={
            "sub_id": str(subscription.id),
            "type": body.renew_type,
            "amount": body.amount,
            "source": "miniapp",
        },
    )
    await apply_renewal(
        session=session,
        subscription=subscription,
        renew_type=body.renew_type,
        amount=body.amount,
    )
    await session.refresh(user.wallet)
    return RenewalResponse(
        status="renewed",
        message="تمدید با موفقیت انجام شد.",
        price=price,
        balance=user.wallet.balance,
    )


async def _get_user_subscription(
    session: AsyncSession,
    user: User,
    subscription_id: UUID,
) -> Subscription:
    subscription = await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.xui_client))
        .where(
            Subscription.id == subscription_id,
            Subscription.user_id == user.id,
        )
    )
    if subscription is None:
        raise HTTPException(status_code=404, detail="سرویس پیدا نشد.")
    return subscription


def _validate_renewal_request(subscription: Subscription, renew_type: str, amount: float) -> None:
    if subscription.status not in {"active", "pending_activation", "expired"}:
        raise HTTPException(status_code=400, detail="این سرویس قابل تمدید نیست.")
    if subscription.plan_id is None:
        raise HTTPException(status_code=400, detail="پلن این سرویس حذف شده و قابل تمدید نیست.")
    if renew_type not in {"volume", "time"}:
        raise HTTPException(status_code=400, detail="نوع تمدید نامعتبر است.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="مقدار تمدید باید بیشتر از صفر باشد.")


# ─── Wallet Transactions ────────────────────────────────────────────────────

@router.get("/wallet/transactions", response_model=TransactionListResponse)
async def get_wallet_transactions(
    page: int = 1,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> TransactionListResponse:
    user, session = auth
    if user.wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found.")

    page_size = 20
    offset = (max(page, 1) - 1) * page_size

    total = await session.scalar(
        select(func.count()).select_from(WalletTransaction)
        .where(WalletTransaction.wallet_id == user.wallet.id)
    ) or 0

    result = await session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.wallet_id == user.wallet.id)
        .order_by(WalletTransaction.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    txs = list(result.scalars().all())

    return TransactionListResponse(
        transactions=[
            TransactionView(
                id=tx.id,
                type=tx.type,
                direction=tx.direction,
                amount=tx.amount,
                currency=tx.currency,
                balance_before=tx.balance_before,
                balance_after=tx.balance_after,
                description=tx.description,
                created_at=tx.created_at,
            )
            for tx in txs
        ],
        total=total,
    )


# ─── Tickets ─────────────────────────────────────────────────────────────────

@router.get("/tickets", response_model=TicketListResponse)
async def get_tickets(
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> TicketListResponse:
    user, session = auth
    result = await session.execute(
        select(Ticket)
        .options(selectinload(Ticket.messages))
        .where(Ticket.user_id == user.id)
        .order_by(Ticket.created_at.desc())
        .limit(20)
    )
    tickets = list(result.scalars().all())

    return TicketListResponse(
        tickets=[
            TicketView(
                id=t.id,
                status=t.status,
                created_at=t.created_at,
                messages=[
                    TicketMessageView(
                        sender_type="user" if m.sender_id == user.id else "admin",
                        text=m.text,
                        photo_id=m.photo_id,
                        created_at=m.created_at,
                    )
                    for m in sorted(t.messages, key=lambda x: x.created_at)
                ],
            )
            for t in tickets
        ]
    )


@router.post("/tickets/send")
async def send_ticket_message(
    body: SendTicketRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict:
    user, session = auth
    repo = TicketRepository(session)
    ticket = await repo.get_open_ticket_for_user(user.id)

    if ticket is None:
        ticket = await repo.create_ticket(user_id=user.id, status="open")

    if ticket.status == "answered":
        ticket.status = "open"

    await repo.add_message(
        ticket_id=ticket.id,
        sender_id=user.id,
        text=body.text.strip(),
    )
    return {"ok": True, "ticket_id": str(ticket.id)}


# ─── Referral ────────────────────────────────────────────────────────────────

@router.get("/referral", response_model=ReferralView)
async def get_referral(
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> ReferralView:
    user, session = auth
    from repositories.settings import AppSettingsRepository

    ref_settings = await AppSettingsRepository(session).get_referral_settings()

    referral_count = await session.scalar(
        select(func.count()).select_from(User)
        .where(User.referred_by_user_id == user.id)
    ) or 0

    # Approximate total earned from referral bonus transactions
    total_earned = await session.scalar(
        select(func.coalesce(func.sum(WalletTransaction.amount), 0))
        .where(
            WalletTransaction.user_id == user.id,
            WalletTransaction.type == "referral_bonus",
        )
    ) or 0

    return ReferralView(
        ref_code=user.ref_code,
        referral_count=referral_count,
        total_earned=total_earned,
        enabled=ref_settings.enabled,
    )


# ─── Telegram Init Data Validation ──────────────────────────────────────────

def validate_telegram_init_data(init_data: str) -> int:
    parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = parsed_data.pop("hash", None)

    if not provided_hash:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Telegram hash.")

    auth_date_raw = parsed_data.get("auth_date")
    if auth_date_raw is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth_date.")

    try:
        auth_date = datetime.fromtimestamp(int(auth_date_raw), tz=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth_date.") from exc

    if (datetime.now(timezone.utc) - auth_date).total_seconds() > 24 * 60 * 60:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Init data expired.")

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=settings.bot_token.get_secret_value().encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed_data.items())
    )
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, provided_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature.")

    user_payload_raw = parsed_data.get("user")
    if user_payload_raw is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user payload.")

    try:
        user_payload = json.loads(user_payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user payload.") from exc

    raw_id = user_payload.get("id")
    if raw_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user id.")

    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user id.") from exc
