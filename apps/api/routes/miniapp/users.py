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
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import SecretStr
from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.dependencies.db import get_db_session
from core.config import settings
from core.miniapp_auth import verify_miniapp_session_token
from models.plan import Plan
from models.ready_config import ReadyConfigItem, ReadyConfigPool
from models.audit import AuditLog
from models.discount import DiscountCode
from models.order import Order
from models.payment import Payment
from models.subscription import Subscription
from models.ticket import Ticket, TicketMessage
from models.user import User
from models.wallet import WalletTransaction
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from repositories.ticket import TicketRepository
from schemas.api.miniapp import (
    MiniAppConfigResponse,
    MiniAppDashboardResponse,
    MiniAppAdminOverviewResponse,
    AdminModuleView,
    PaymentListResponse,
    PaymentView,
    PlanListResponse,
    PlanView,
    CustomPurchaseView,
    PurchaseRequest,
    PurchaseResponse,
    ReferralView,
    RenewalQuoteRequest,
    RenewalQuoteResponse,
    RenewalRequest,
    RenewalResponse,
    SubscriptionListResponse,
    SendTicketRequest,
    SubscriptionView,
    TicketListResponse,
    TicketMessageView,
    TicketView,
    TopUpRequest,
    TopUpResponse,
    TransactionListResponse,
    TransactionView,
    WalletView,
)
from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError
from services.admin_gifts import grant_bulk_subscription_gift
from services.custom_purchase import (
    CustomPurchaseError,
    calculate_custom_purchase_price,
    create_custom_purchase_plan,
    get_custom_purchase_template_plan,
)
from services.payment import review_gateway_payment
from services.plan_inventory import (
    PlanStockError,
    ensure_plan_available,
    get_effective_plan_stock_map,
    get_plan_stock_map,
    is_stock_available,
    set_plan_sales_limit,
)
from services.phone_verification import get_verified_phone
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.renewal import apply_renewal, calculate_renewal_price
from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError
from services.tronado.payments import create_tronado_invoice
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
            selectinload(User.profile),
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

    active_statuses = ("active", "pending_activation")
    active_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(active_statuses),
        )
    ) or 0
    total_used = await session.scalar(
        select(func.coalesce(func.sum(Subscription.used_bytes), 0)).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(active_statuses),
        )
    ) or 0
    total_vol = await session.scalar(
        select(func.coalesce(func.sum(Subscription.volume_bytes), 0)).where(
            Subscription.user_id == user.id,
            Subscription.status.in_(active_statuses),
        )
    ) or 0

    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(active_statuses),
        )
        .order_by(Subscription.created_at.desc())
        .limit(5)
    )
    subs = [_subscription_to_view(sub) for sub in result.scalars().all()]

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


@router.get("/configs", response_model=SubscriptionListResponse)
async def get_configs(
    page: int = 1,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> SubscriptionListResponse:
    user, session = auth
    page_size = 20
    page = max(page, 1)

    total = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    ) or 0
    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return SubscriptionListResponse(
        subscriptions=[_subscription_to_view(sub) for sub in result.scalars().all()],
        total=total,
        page=page,
        page_size=page_size,
    )


def _subscription_to_view(sub: Subscription) -> SubscriptionView:
    return SubscriptionView(
        id=sub.id,
        status=sub.status,
        used_bytes=sub.used_bytes,
        volume_bytes=sub.volume_bytes,
        sub_link=sub.sub_link,
        plan_name=sub.plan.name if sub.plan else None,
        plan_price=sub.plan.price if sub.plan else None,
        plan_duration_days=sub.plan.duration_days if sub.plan else None,
        starts_at=sub.starts_at,
        ends_at=sub.ends_at,
        config_name=sub.xui_client.username if sub.xui_client else None,
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
        select(func.count())
        .select_from(Plan)
        .where(Plan.is_active.is_(True), not_(Plan.code.like("custom\\_%", escape="\\")))
    ) or 0

    modules = [
        AdminModuleView(title="آمار و گزارش‌ها", description="وضعیت فروش، کاربران و سرویس‌ها", callback="admin:stats"),
        AdminModuleView(title="مدیریت مالی", description="بازبینی پرداخت‌ها، پرداخت دستی و ریکاوری", callback="admin:finance"),
        AdminModuleView(title="کاربران", description="لیست، جستجو، موجودی، بن و نقش‌ها", callback="admin:users"),
        AdminModuleView(title="مشتریان", description="کاربران خریدار و کانفیگ‌هایشان", callback="admin:customers"),
        AdminModuleView(title="سرویس‌ها", description="اشتراک‌ها، کانفیگ‌ها و وضعیت‌ها", callback="admin:subs"),
        AdminModuleView(title="هدیه گروهی", description="افزایش زمان یا حجم برای گروهی از کانفیگ‌ها", callback="admin:gifts"),
        AdminModuleView(title="پلن‌ها", description="ساخت، مشاهده و فعال/غیرفعال کردن پلن‌ها", callback="admin:plans"),
        AdminModuleView(title="فروش کانفیگ آماده", description="موجودی فایل‌های آماده و تحویل خودکار", callback="admin:ready_configs"),
        AdminModuleView(title="سرورها", description="مدیریت سرورهای X-UI و اینباندها", callback="admin:servers"),
        AdminModuleView(title="تیکت‌ها", description="بررسی و پاسخ به پشتیبانی", callback="admin:tickets"),
        AdminModuleView(title="تخفیف‌ها", description="ساخت و مدیریت کدهای تخفیف", callback="admin:discounts"),
        AdminModuleView(title="تنظیمات ربات", description="درگاه‌ها، تست کانفیگ، ریفرال و نرخ‌ها", callback="admin:settings"),
        AdminModuleView(title="ادیت لاگ", description="ردیابی اکشن‌های حساس مدیریت", callback="admin:audit"),
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


@router.get("/admin/section/{section}")
async def get_admin_section(
    section: str,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)

    if section == "stats":
        return {"title": "آمار و گزارش‌ها", "items": await _admin_stats(session)}
    if section == "finance":
        payments = await _admin_payments(session)
        return {"title": "مدیریت مالی", "items": payments}
    if section in {"users", "customers"}:
        users = await _admin_users(session, customers_only=section == "customers")
        return {"title": "کاربران" if section == "users" else "مشتریان", "items": users}
    if section == "subs":
        return {"title": "سرویس‌ها", "items": await _admin_subscriptions(session)}
    if section == "gifts":
        return {"title": "هدیه گروهی", "items": await _admin_gift_options(session)}
    if section == "plans":
        return {"title": "پلن‌ها", "items": await _admin_plans(session)}
    if section == "ready_configs":
        return {"title": "فروش کانفیگ آماده", "items": await _admin_ready_configs(session)}
    if section == "servers":
        return {"title": "سرورها", "items": await _admin_servers(session)}
    if section == "tickets":
        return {"title": "تیکت‌ها", "items": await _admin_tickets(session)}
    if section == "discounts":
        return {"title": "تخفیف‌ها", "items": await _admin_discounts(session)}
    if section == "settings":
        return {"title": "تنظیمات", "items": await _admin_settings(session)}
    if section == "audit":
        return {"title": "ادیت لاگ", "items": await _admin_audit_logs(session)}
    if section in {"broadcast", "retargeting", "backup"}:
        return {
            "title": {
                "broadcast": "پیام همگانی",
                "retargeting": "ریتارگتینگ",
                "backup": "بکاپ",
            }[section],
            "items": [
                {
                    "id": section,
                    "title": "این بخش نیازمند ورودی چندمرحله‌ای است",
                    "subtitle": "نسخه وب در حال آماده‌سازی است؛ فعلاً از پنل ربات استفاده کنید.",
                    "actions": [],
                }
            ],
        }
    raise HTTPException(status_code=404, detail="بخش مدیریت پیدا نشد.")


@router.get("/admin/reports/customers")
async def get_admin_customer_report(
    period: str = "daily",
    user_id: UUID | None = None,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    period = period.strip().lower()
    if period not in {"daily", "weekly"}:
        raise HTTPException(status_code=400, detail="دوره گزارش باید daily یا weekly باشد.")
    now = datetime.now(timezone.utc)
    start = now - (timedelta(days=7) if period == "weekly" else timedelta(days=1))
    stmt = (
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.plan))
        .where(Subscription.created_at >= start)
        .order_by(Subscription.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(Subscription.user_id == user_id)
    result = await session.execute(stmt)
    rows: dict[UUID, dict[str, Any]] = {}
    total_bytes = 0
    total_amount = Decimal("0")
    for sub in result.scalars().all():
        total_bytes += int(sub.volume_bytes or 0)
        price = Decimal(str(sub.plan.price if sub.plan else 0))
        total_amount += price
        item = rows.setdefault(
            sub.user_id,
            {
                "user_id": str(sub.user_id),
                "telegram_id": sub.user.telegram_id if sub.user else None,
                "name": (sub.user.first_name or sub.user.username or str(sub.user.telegram_id)) if sub.user else "-",
                "configs_count": 0,
                "volume_gb": 0.0,
                "amount_usd": "0.00",
            },
        )
        item["configs_count"] += 1
        item["volume_gb"] = round(float(item["volume_gb"]) + _bytes_to_gb(sub.volume_bytes), 2)
        item["amount_usd"] = str((Decimal(str(item["amount_usd"])) + price).quantize(Decimal("0.01")))
    return {
        "period": period,
        "from": start.isoformat(),
        "to": now.isoformat(),
        "total_customers": len(rows),
        "total_configs": sum(item["configs_count"] for item in rows.values()),
        "total_volume_gb": round(_bytes_to_gb(total_bytes), 2),
        "total_amount_usd": str(total_amount.quantize(Decimal("0.01"))),
        "items": sorted(rows.values(), key=lambda item: item["volume_gb"], reverse=True),
    }


@router.post("/admin/action")
async def post_admin_action(
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    action = str(body.get("action") or "")
    target_id_raw = str(body.get("id") or "")

    try:
        target_id = UUID(target_id_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="شناسه نامعتبر است.") from exc

    if action == "toggle_plan":
        plan = await session.get(Plan, target_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="پلن پیدا نشد.")
        plan.is_active = not plan.is_active
        _record_admin_action(session, user, action, "plan", plan.id, {"is_active": plan.is_active})
        await session.flush()
        return {"ok": True, "message": "وضعیت پلن تغییر کرد."}

    if action == "toggle_server":
        server = await session.get(XUIServerRecord, target_id)
        if server is None:
            raise HTTPException(status_code=404, detail="سرور پیدا نشد.")
        server.is_active = not server.is_active
        _record_admin_action(session, user, action, "server", server.id, {"is_active": server.is_active})
        await session.flush()
        return {"ok": True, "message": "وضعیت سرور تغییر کرد."}

    if action == "toggle_ready_pool":
        pool = await session.get(ReadyConfigPool, target_id)
        if pool is None:
            raise HTTPException(status_code=404, detail="موجودی کانفیگ آماده پیدا نشد.")
        pool.is_active = not pool.is_active
        _record_admin_action(session, user, action, "ready_config_pool", pool.id, {"is_active": pool.is_active})
        await session.flush()
        return {"ok": True, "message": "وضعیت فروش کانفیگ آماده تغییر کرد."}

    if action == "toggle_discount":
        discount = await session.get(DiscountCode, target_id)
        if discount is None:
            raise HTTPException(status_code=404, detail="کد تخفیف پیدا نشد.")
        discount.is_active = not discount.is_active
        _record_admin_action(session, user, action, "discount", discount.id, {"is_active": discount.is_active})
        await session.flush()
        return {"ok": True, "message": "وضعیت تخفیف تغییر کرد."}

    if action == "toggle_user_ban":
        target = await session.get(User, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
        target.status = "active" if target.status == "banned" else "banned"
        target.is_bot_blocked = target.status == "banned"
        disabled_count = 0
        if target.status == "banned":
            disabled_count = await _disable_user_configs_for_ban(session, target.id)
        _record_admin_action(session, user, action, "user", target.id, {"status": target.status, "disabled_configs": disabled_count})
        await session.flush()
        return {"ok": True, "message": f"وضعیت کاربر تغییر کرد. {disabled_count} کانفیگ غیرفعال شد."}

    if action == "toggle_user_role":
        target = await session.get(User, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
        if target.telegram_id == settings.owner_telegram_id or target.role == "owner":
            raise HTTPException(status_code=400, detail="نقش مالک اصلی قابل تغییر نیست.")
        target.role = "admin" if target.role == "user" else "user"
        _record_admin_action(session, user, action, "user", target.id, {"role": target.role})
        await session.flush()
        return {"ok": True, "message": "نقش کاربر تغییر کرد."}

    if action == "reset_trial":
        target = await session.get(User, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
        target.has_received_free_trial = False
        _record_admin_action(session, user, action, "user", target.id, {"has_received_free_trial": False})
        await session.flush()
        return {"ok": True, "message": "محدودیت تست کاربر ریست شد."}

    if action == "review_payment":
        payment = await session.get(Payment, target_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="پرداخت پیدا نشد.")
        try:
            result = await review_gateway_payment(session, payment)
        except Exception as exc:
            logger.error("Admin review_payment failed for %s: %s", target_id, exc, exc_info=True)
            raise HTTPException(
                status_code=502,
                detail=f"خطا در بازبینی پرداخت: {type(exc).__name__}: {exc}",
            ) from exc
        _record_admin_action(session, user, action, "payment", payment.id, {"result": result})
        return {"ok": True, "message": f"نتیجه بازبینی: {result}"}

    if action == "close_ticket":
        ticket = await session.get(Ticket, target_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="تیکت پیدا نشد.")
        ticket.status = "closed"
        _record_admin_action(session, user, action, "ticket", ticket.id, {"status": "closed"})
        await session.flush()
        return {"ok": True, "message": "تیکت بسته شد."}

    raise HTTPException(status_code=400, detail="اکشن نامعتبر است.")


@router.post("/admin/plans/{plan_id}/duration")
async def update_admin_plan_duration(
    plan_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن پیدا نشد.")
    try:
        duration_days = int(body.get("duration_days") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="مدت پلن معتبر نیست.") from exc
    if duration_days <= 0:
        raise HTTPException(status_code=400, detail="مدت پلن باید بیشتر از صفر باشد.")
    old_duration = plan.duration_days
    plan.duration_days = duration_days
    _record_admin_action(
        session,
        user,
        "edit_plan_duration",
        "plan",
        plan.id,
        {"from": old_duration, "to": duration_days},
    )
    await session.flush()
    return {"ok": True, "message": "مدت پلن تغییر کرد.", "duration_days": duration_days}


@router.post("/admin/plans/{plan_id}/name")
async def update_admin_plan_name(
    plan_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن پیدا نشد.")
    name = str(body.get("name") or "").strip()
    if len(name) < 2 or len(name) > 80:
        raise HTTPException(status_code=400, detail="نام پلن باید بین ۲ تا ۸۰ کاراکتر باشد.")
    old_name = plan.name
    plan.name = name
    _record_admin_action(session, user, "edit_plan_name", "plan", plan.id, {"from": old_name, "to": name})
    await session.flush()
    return {"ok": True, "message": "نام پلن تغییر کرد.", "name": name}


@router.post("/admin/plans/{plan_id}/price")
async def update_admin_plan_price(
    plan_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن پیدا نشد.")
    try:
        price = Decimal(str(body.get("price") or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="قیمت پلن معتبر نیست.") from exc
    if price <= Decimal("0"):
        raise HTTPException(status_code=400, detail="قیمت پلن باید بیشتر از صفر باشد.")
    old_price = plan.price
    old_renewal_price = plan.renewal_price
    plan.price = price
    plan.renewal_price = price
    _record_admin_action(
        session,
        user,
        "edit_plan_price",
        "plan",
        plan.id,
        {
            "from": str(old_price),
            "to": str(price),
            "renewal_price_from": str(old_renewal_price),
            "renewal_price_to": str(price),
        },
    )
    await session.flush()
    return {"ok": True, "message": "قیمت پلن تغییر کرد.", "price": str(price)}


@router.post("/admin/plans/{plan_id}/stock")
async def update_admin_plan_stock(
    plan_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="پلن پیدا نشد.")
    try:
        sales_limit = int(body.get("sales_limit") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="موجودی پلن معتبر نیست.") from exc
    stock = await set_plan_sales_limit(session, plan.id, sales_limit)
    _record_admin_action(
        session,
        user,
        "edit_plan_stock",
        "plan",
        plan.id,
        {
            "sales_limit": stock.sales_limit,
            "sold_count": stock.sold_count,
            "stock_remaining": stock.stock_remaining,
        },
    )
    await session.flush()
    label = "نامحدود" if stock.is_unlimited else f"{stock.stock_remaining} باقی‌مانده"
    return {
        "ok": True,
        "message": f"موجودی پلن تنظیم شد: {label}",
        "stock": {
            "sales_limit": stock.sales_limit,
            "sold_count": stock.sold_count,
            "stock_remaining": stock.stock_remaining,
            "is_unlimited": stock.is_unlimited,
        },
    }


@router.post("/admin/servers/{server_id}/sub-scheme")
async def update_admin_server_sub_scheme(
    server_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    server = await session.get(XUIServerRecord, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="سرور پیدا نشد.")
    scheme = str(body.get("scheme") or "").strip().lower()
    if scheme not in {"http", "https", "panel"}:
        raise HTTPException(status_code=400, detail="نوع لینک باید http، https یا panel باشد.")
    if scheme == "panel":
        scheme = "https" if server.base_url.strip().lower().startswith("https://") else "http"
    metadata = dict(server.metadata_ or {})
    old_scheme = metadata.get("subscription_scheme")
    metadata["subscription_scheme"] = scheme
    server.metadata_ = metadata
    _record_admin_action(
        session,
        user,
        "edit_server_sub_scheme",
        "server",
        server.id,
        {"from": old_scheme, "to": scheme},
    )
    await session.flush()
    return {"ok": True, "message": f"نوع لینک ساب روی {scheme} تنظیم شد.", "scheme": scheme}


@router.post("/admin/custom-purchase")
async def update_admin_custom_purchase(
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    payload: dict[str, Any] = {}
    if "enabled" in body:
        payload["enabled"] = bool(body.get("enabled"))
    if "price_per_gb" in body:
        try:
            price_per_gb = float(str(body.get("price_per_gb")).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="قیمت هر گیگ معتبر نیست.") from exc
        if price_per_gb <= 0:
            raise HTTPException(status_code=400, detail="قیمت هر گیگ باید بیشتر از صفر باشد.")
        payload["price_per_gb"] = price_per_gb
    if "price_per_day" in body:
        try:
            price_per_day = float(str(body.get("price_per_day")).replace(",", "."))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="قیمت هر روز معتبر نیست.") from exc
        if price_per_day <= 0:
            raise HTTPException(status_code=400, detail="قیمت هر روز باید بیشتر از صفر باشد.")
        payload["price_per_day"] = price_per_day

    if not payload:
        raise HTTPException(status_code=400, detail="مقداری برای بروزرسانی ارسال نشده است.")

    settings = await AppSettingsRepository(session).update_custom_purchase_settings(**payload)
    _record_admin_action(
        session,
        user,
        "edit_custom_purchase_settings",
        "app_setting",
        None,
        {
            "enabled": settings.enabled,
            "price_per_gb": settings.price_per_gb,
            "price_per_day": settings.price_per_day,
        },
    )
    await session.flush()
    return {
        "ok": True,
        "message": "تنظیمات خرید دلخواه بروزرسانی شد.",
        "settings": {
            "enabled": settings.enabled,
            "price_per_gb": settings.price_per_gb,
            "price_per_day": settings.price_per_day,
        },
    }


@router.post("/admin/gifts")
async def grant_admin_bulk_gift(
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    gift_type = str(body.get("gift_type") or "")
    status_scope = str(body.get("status_scope") or "")
    server_id_raw = body.get("server_id")
    try:
        amount = float(body.get("amount") or 0)
        server_id = UUID(str(server_id_raw)) if server_id_raw else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="اطلاعات هدیه معتبر نیست.") from exc
    if gift_type == "time" and int(amount) != amount:
        raise HTTPException(status_code=400, detail="هدیه زمان باید عدد صحیح روز باشد.")

    try:
        result = await grant_bulk_subscription_gift(
            session=session,
            gift_type=gift_type,
            amount=amount,
            status_scope=status_scope,
            server_id=server_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="اطلاعات هدیه معتبر نیست.") from exc

    _record_admin_action(
        session,
        user,
        "bulk_subscription_gift",
        "subscription",
        user.id,
        {
            "gift_type": gift_type,
            "amount": amount,
            "status_scope": status_scope,
            "server_id": str(server_id) if server_id else None,
            "matched": result.matched_count,
            "updated": result.updated_count,
            "failed": result.failed_count,
        },
    )
    await session.flush()
    return {
        "ok": True,
        "message": f"هدیه اعمال شد. موفق: {result.updated_count} از {result.matched_count}",
        "matched_count": result.matched_count,
        "updated_count": result.updated_count,
        "failed_count": result.failed_count,
    }


@router.post("/admin/ready-configs/plans")
async def create_ready_config_plan(
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)

    name = str(body.get("name") or "").strip()
    try:
        duration_days = int(body.get("duration_days") or 0)
        volume_gb = int(body.get("volume_gb") or 0)
        price = Decimal(str(body.get("price") or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=400, detail="اطلاعات پلن معتبر نیست.") from exc

    if not name or duration_days <= 0 or volume_gb <= 0 or price <= 0:
        raise HTTPException(status_code=400, detail="نام، مدت، حجم و قیمت باید معتبر باشند.")

    plan = Plan(
        code=f"ready_{duration_days}d_{volume_gb}gb_{price.normalize()}_{uuid4().hex[:8]}",
        name=name,
        protocol="ready_config",
        inbound_id=None,
        duration_days=duration_days,
        volume_bytes=volume_gb * 1024 * 1024 * 1024,
        price=price,
        renewal_price=price,
        currency="USD",
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    pool = ReadyConfigPool(plan_id=plan.id, is_active=True)
    session.add(pool)
    _record_admin_action(
        session,
        user,
        "create_ready_config_plan",
        "plan",
        plan.id,
        {"pool_id": str(pool.id), "volume_gb": volume_gb, "price": str(price)},
    )
    await session.flush()
    return {"ok": True, "message": "پلن آماده ساخته شد.", "pool_id": str(pool.id), "plan_id": str(plan.id)}


@router.post("/admin/ready-configs/{pool_id}/items")
async def add_ready_config_items(
    pool_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    pool = await session.get(ReadyConfigPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="موجودی کانفیگ آماده پیدا نشد.")

    lines = [line.strip() for line in str(body.get("content") or "").splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="حداقل یک کانفیگ وارد کنید.")

    existing = set(await session.scalars(select(ReadyConfigItem.content).where(ReadyConfigItem.pool_id == pool_id)))
    created = 0
    for index, line in enumerate(lines, start=1):
        if line in existing:
            continue
        session.add(
            ReadyConfigItem(
                pool_id=pool_id,
                content=line,
                status="available",
                source_name="miniapp",
                line_number=index,
            )
        )
        existing.add(line)
        created += 1

    _record_admin_action(
        session,
        user,
        "add_ready_config_items",
        "ready_config_pool",
        pool.id,
        {"received": len(lines), "created": created},
    )
    await session.flush()
    return {"ok": True, "message": f"{created} کانفیگ جدید اضافه شد.", "created": created}


@router.get("/admin/users/search")
async def search_admin_users(
    q: str = "",
    page: int = 1,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    query_text = q.strip().lstrip("@")
    page = max(page, 1)
    page_size = 20

    stmt = select(User).options(selectinload(User.wallet))
    count_stmt = select(func.count()).select_from(User)
    if query_text:
        filters = [
            User.username.ilike(f"%{query_text}%"),
            User.first_name.ilike(f"%{query_text}%"),
            User.last_name.ilike(f"%{query_text}%"),
        ]
        if query_text.isdigit():
            filters.append(User.telegram_id == int(query_text))
        stmt = stmt.where(or_(*filters))
        count_stmt = count_stmt.where(or_(*filters))

    total = int(await session.scalar(count_stmt) or 0)
    result = await session.execute(
        stmt.order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    users = list(result.scalars().unique().all())
    return {
        "items": [_serialize_admin_user_summary(u) for u in users],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": page * page_size < total,
    }


@router.get("/admin/users/{target_user_id}")
async def get_admin_user_detail(
    target_user_id: UUID,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    admin, session = auth
    _require_admin(admin)
    target = await session.scalar(
        select(User)
        .options(
            selectinload(User.wallet),
            selectinload(User.profile),
            selectinload(User.subscriptions).selectinload(Subscription.plan),
            selectinload(User.subscriptions).selectinload(Subscription.xui_client),
            selectinload(User.orders),
        )
        .where(User.id == target_user_id)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")
    return _serialize_admin_user_detail(target)


@router.post("/admin/users/{target_user_id}/balance")
async def adjust_admin_user_balance(
    target_user_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    admin, session = auth
    _require_admin(admin)
    try:
        amount = Decimal(str(body.get("amount") or "0"))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="مبلغ معتبر نیست.") from exc
    if amount == Decimal("0"):
        raise HTTPException(status_code=400, detail="مبلغ نمی‌تواند صفر باشد.")

    target = await session.scalar(
        select(User)
        .options(
            selectinload(User.wallet),
            selectinload(User.profile),
            selectinload(User.subscriptions).selectinload(Subscription.plan),
            selectinload(User.subscriptions).selectinload(Subscription.xui_client),
            selectinload(User.orders),
        )
        .where(User.id == target_user_id)
    )
    if target is None or target.wallet is None:
        raise HTTPException(status_code=404, detail="کاربر یا کیف پول پیدا نشد.")

    direction = "credit" if amount > 0 else "debit"
    try:
        await WalletManager(session).process_transaction(
            user_id=target.id,
            amount=abs(amount),
            transaction_type="admin_adjust",
            direction=direction,
            currency="USD",
            reference_type="admin_user",
            reference_id=admin.id,
            description="Mini App admin wallet adjustment",
            metadata={"admin_id": str(admin.id)},
        )
    except InsufficientBalanceError as exc:
        raise HTTPException(status_code=400, detail="موجودی کاربر برای کسر کافی نیست.") from exc

    _record_admin_action(
        session,
        admin,
        "adjust_balance",
        "user",
        target.id,
        {"amount": str(amount), "telegram_id": target.telegram_id},
    )
    await session.flush()
    await session.refresh(target.wallet)
    return {"ok": True, "message": "موجودی کاربر تغییر کرد.", "user": _serialize_admin_user_detail(target)}


@router.post("/admin/users/{target_user_id}/message")
async def send_admin_user_message(
    target_user_id: UUID,
    body: dict[str, Any],
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    admin, session = auth
    _require_admin(admin)
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="متن پیام خالی است.")
    target = await session.get(User, target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد.")

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
    )
    try:
        await bot.send_message(target.telegram_id, f"پیام از طرف مدیریت:\n\n{text}", parse_mode=None)
    except TelegramForbiddenError as exc:
        target.is_bot_blocked = True
        raise HTTPException(status_code=400, detail="کاربر ربات را بلاک کرده است.") from exc
    except TelegramBadRequest as exc:
        raise HTTPException(status_code=400, detail=f"خطا در ارسال پیام: {exc}") from exc
    finally:
        await bot.session.close()

    _record_admin_action(session, admin, "send_message", "user", target.id, {"telegram_id": target.telegram_id})
    await session.flush()
    return {"ok": True, "message": "پیام برای کاربر ارسال شد."}


@router.get("/admin/tickets/{ticket_id}")
async def get_admin_ticket(
    ticket_id: UUID,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    _require_admin(user)
    ticket = await TicketRepository(session).get_ticket_with_messages(ticket_id)
    if ticket is None or ticket.user is None:
        raise HTTPException(status_code=404, detail="تیکت پیدا نشد.")
    return _serialize_ticket_for_admin(ticket)


@router.post("/admin/tickets/{ticket_id}/reply")
async def reply_admin_ticket(
    ticket_id: UUID,
    body: SendTicketRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    admin, session = auth
    _require_admin(admin)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="متن پاسخ خالی است.")

    repo = TicketRepository(session)
    ticket = await repo.get_ticket_with_messages(ticket_id)
    if ticket is None or ticket.user is None:
        raise HTTPException(status_code=404, detail="تیکت پیدا نشد.")
    if ticket.status == "closed":
        raise HTTPException(status_code=400, detail="این تیکت بسته شده است.")

    await repo.add_message(ticket_id=ticket.id, sender_id=admin.id, text=text)
    ticket.status = "answered"
    _record_admin_action(session, admin, "reply_ticket", "ticket", ticket.id, {"user_id": str(ticket.user_id)})

    delivered = await _notify_ticket_user(ticket.user.telegram_id, ticket.id, text)
    if not delivered:
        ticket.user.is_bot_blocked = True

    await session.flush()
    ticket = await repo.get_ticket_with_messages(ticket.id)
    return {
        "ok": True,
        "message": "پاسخ ثبت شد و برای کاربر ارسال شد." if delivered else "پاسخ ثبت شد، اما ارسال پیام تلگرام به کاربر انجام نشد.",
        "ticket": _serialize_ticket_for_admin(ticket) if ticket else None,
    }


def _require_admin(user: User) -> None:
    if not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="دسترسی مدیریت ندارید.")


def _bytes_to_gb(value: int | None) -> float:
    return float(value or 0) / float(1024**3)


def _record_admin_action(
    session: AsyncSession,
    actor: User,
    action: str,
    entity_type: str,
    entity_id: UUID | None,
    payload: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload or {},
        )
    )


async def _disable_user_configs_for_ban(session: AsyncSession, user_id: UUID) -> int:
    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
        )
        .where(
            Subscription.user_id == user_id,
            Subscription.status.in_(["active", "pending_activation"]),
        )
    )
    disabled_count = 0
    manager = ProvisioningManager(session)
    for subscription in result.scalars().all():
        subscription.status = "disabled"
        xui_record = subscription.xui_client
        if xui_record is not None:
            xui_record.is_active = False
            try:
                await manager._disable_xui_client(
                    xui_record=xui_record,
                    volume_bytes=subscription.volume_bytes,
                    ends_at=subscription.ends_at,
                )
            except Exception as exc:
                logger.warning("Could not disable remote config %s for banned user %s: %s", xui_record.id, user_id, exc)
        disabled_count += 1
    return disabled_count


def _serialize_ticket_for_admin(ticket: Ticket) -> dict[str, Any]:
    ticket_user = ticket.user
    return {
        "id": str(ticket.id),
        "status": ticket.status,
        "user": {
            "id": str(ticket_user.id) if ticket_user else None,
            "telegram_id": ticket_user.telegram_id if ticket_user else None,
            "name": (ticket_user.first_name or ticket_user.username or str(ticket_user.telegram_id)) if ticket_user else "-",
            "username": ticket_user.username if ticket_user else None,
        },
        "created_at": ticket.created_at,
        "messages": [
            {
                "sender_type": "user" if msg.sender_id == ticket.user_id else "admin",
                "text": msg.text,
                "photo_id": msg.photo_id,
                "created_at": msg.created_at,
            }
            for msg in sorted(ticket.messages, key=lambda item: item.created_at)
        ],
    }


async def _notify_ticket_user(telegram_id: int, ticket_id: UUID, text: str) -> bool:
    bot = Bot(token=settings.bot_token.get_secret_value())
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=f"پاسخ پشتیبانی برای تیکت #{str(ticket_id)[:8]}:\n\n{text}",
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False
    finally:
        await bot.session.close()


async def _admin_stats(session: AsyncSession) -> list[dict[str, Any]]:
    return [
        {"title": "کل کاربران", "value": int(await session.scalar(select(func.count()).select_from(User)) or 0)},
        {"title": "سرویس‌های فعال", "value": int(await session.scalar(select(func.count()).select_from(Subscription).where(Subscription.status.in_(["active", "pending_activation"]))) or 0)},
        {"title": "پلن‌های فعال", "value": int(await session.scalar(select(func.count()).select_from(Plan).where(Plan.is_active.is_(True), not_(Plan.code.like("custom\\_%", escape="\\")))) or 0)},
        {"title": "پرداخت‌های منتظر", "value": int(await session.scalar(select(func.count()).select_from(Payment).where(Payment.payment_status.in_(["waiting", "pending"]))) or 0)},
    ]


async def _admin_payments(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(Payment).order_by(Payment.created_at.desc()).limit(30)
    )
    return [
        {
            "id": str(p.id),
            "title": f"{p.provider} | {p.kind}",
            "subtitle": f"{p.payment_status} | {p.price_amount} {p.price_currency}",
            "actions": [{"label": "بازبینی", "action": "review_payment"}] if p.provider in {"nowpayments", "tetrapay", "tronado"} else [],
        }
        for p in result.scalars().all()
    ]


async def _admin_users(session: AsyncSession, *, customers_only: bool = False) -> list[dict[str, Any]]:
    stmt = select(User).options(selectinload(User.wallet)).order_by(User.created_at.desc()).limit(30)
    if customers_only:
        stmt = (
            select(User)
            .join(Order, Order.user_id == User.id)
            .options(selectinload(User.wallet))
            .where(Order.status.in_(["provisioned", "paid", "completed"]))
            .group_by(User.id)
            .order_by(User.created_at.desc())
            .limit(30)
        )
    result = await session.execute(stmt)
    users = result.scalars().unique().all()
    return [
        {
            "id": str(u.id),
            "title": u.first_name or u.username or str(u.telegram_id),
            "subtitle": f"{u.telegram_id} | {u.role} | {u.status} | ${u.wallet.balance if u.wallet else 0}",
            "actions": [
                {"label": "پروفایل", "action": "view_user"},
                {"label": "بن/رفع بن", "action": "toggle_user_ban"},
                {"label": "ریست تست", "action": "reset_trial"},
            ],
        }
        for u in users
    ]


def _serialize_admin_user_summary(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "telegram_id": user.telegram_id,
        "username": user.username,
        "name": " ".join(part for part in [user.first_name, user.last_name] if part) or user.username or str(user.telegram_id),
        "role": user.role,
        "status": user.status,
        "is_bot_blocked": user.is_bot_blocked,
        "has_received_free_trial": user.has_received_free_trial,
        "wallet_balance": str(user.wallet.balance if user.wallet else Decimal("0")),
        "credit_limit": str(user.wallet.credit_limit if user.wallet else Decimal("0")),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _serialize_admin_user_detail(user: User) -> dict[str, Any]:
    summary = _serialize_admin_user_summary(user)
    summary["phone"] = get_verified_phone(user)
    subscriptions = []
    for sub in sorted(user.subscriptions or [], key=lambda item: item.created_at, reverse=True):
        subscriptions.append(
            {
                "id": str(sub.id),
                "status": sub.status,
                "plan_name": sub.plan.name if sub.plan else None,
                "config_name": sub.xui_client.username if sub.xui_client else None,
                "used_bytes": sub.used_bytes,
                "volume_bytes": sub.volume_bytes,
                "starts_at": sub.starts_at.isoformat() if sub.starts_at else None,
                "ends_at": sub.ends_at.isoformat() if sub.ends_at else None,
                "sub_link": sub.sub_link,
            }
        )
    summary["subscriptions"] = subscriptions
    summary["orders_count"] = len(user.orders or [])
    return summary


async def _admin_subscriptions(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.plan), selectinload(Subscription.xui_client))
        .order_by(Subscription.created_at.desc())
        .limit(30)
    )
    return [
        {
            "id": str(s.id),
            "title": (s.xui_client.username if s.xui_client else None) or (s.plan.name if s.plan else "سرویس"),
            "subtitle": f"{s.status} | {s.user.telegram_id if s.user else '-'} | {s.used_bytes}/{s.volume_bytes}",
            "actions": [],
        }
        for s in result.scalars().unique().all()
    ]


async def _admin_gift_options(session: AsyncSession) -> list[dict[str, Any]]:
    active_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status.in_(["active", "pending_activation"]))
        )
        or 0
    )
    all_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status.in_(["active", "pending_activation", "expired"]))
        )
        or 0
    )
    result = await session.execute(
        select(XUIServerRecord)
        .where(XUIServerRecord.health_status != "deleted")
        .order_by(XUIServerRecord.created_at.asc())
        .limit(50)
    )
    items = [
        {
            "id": "summary",
            "title": "آماده برای اعمال هدیه",
            "subtitle": f"فعال‌ها: {active_count} | همه قابل هدیه: {all_count}",
            "actions": [],
        }
    ]
    for server in result.scalars().all():
        items.append(
            {
                "id": str(server.id),
                "title": server.name,
                "subtitle": f"{server.health_status} | {'فعال' if server.is_active else 'غیرفعال'}",
                "actions": [],
            }
        )
    return items


def _format_admin_stock(stock: Any) -> str:
    if stock.is_unlimited:
        return "موجودی نامحدود"
    return f"موجودی: {stock.stock_remaining} از {stock.sales_limit}"


async def _admin_plans(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(Plan)
        .where(not_(Plan.code.like("custom\\_%", escape="\\")))
        .order_by(Plan.created_at.desc())
        .limit(50)
    )
    plans = list(result.scalars().all())
    stock_by_plan_id = await get_plan_stock_map(session, [p.id for p in plans])
    return [
        {
            "id": str(p.id),
            "title": p.name,
            "subtitle": (
                f"{p.price} {p.currency} | {p.duration_days} روز | "
                f"{_format_admin_stock(stock_by_plan_id[p.id])} | "
                f"{'فعال' if p.is_active else 'غیرفعال'}"
            ),
            "actions": [
                {"label": "فعال/غیرفعال", "action": "toggle_plan"},
                {"label": "تغییر نام", "action": "edit_plan_name"},
                {"label": "تغییر مدت", "action": "edit_plan_duration"},
                {"label": "تغییر قیمت", "action": "edit_plan_price"},
                {"label": "تنظیم موجودی", "action": "edit_plan_stock"},
            ],
        }
        for p in plans
    ]


async def _admin_ready_configs(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(ReadyConfigPool)
        .options(selectinload(ReadyConfigPool.plan))
        .join(ReadyConfigPool.plan)
        .order_by(ReadyConfigPool.created_at.desc())
        .limit(50)
    )
    pools = list(result.scalars().all())
    items: list[dict[str, Any]] = []
    for pool in pools:
        available = int(
            await session.scalar(
                select(func.count()).select_from(ReadyConfigItem).where(
                    ReadyConfigItem.pool_id == pool.id,
                    ReadyConfigItem.status == "available",
                )
            )
            or 0
        )
        sold = int(
            await session.scalar(
                select(func.count()).select_from(ReadyConfigItem).where(
                    ReadyConfigItem.pool_id == pool.id,
                    ReadyConfigItem.status == "sold",
                )
            )
            or 0
        )
        items.append(
            {
                "id": str(pool.id),
                "title": pool.plan.name,
                "subtitle": (
                    f"آماده: {available} | فروخته: {sold} | "
                    f"{pool.plan.duration_days} روز | {pool.plan.price} {pool.plan.currency}"
                ),
                "actions": [{"label": "فعال/غیرفعال", "action": "toggle_ready_pool"}],
            }
        )
    if not items:
        items.append(
            {
                "id": "ready_configs_help",
                "title": "هنوز پلن آماده‌ای ساخته نشده است",
                "subtitle": "از پنل مدیریت ربات گزینه «فروش کانفیگ آماده» را بزنید، پلن بسازید و فایل txt را آپلود کنید.",
                "actions": [],
            }
        )
    return items


async def _admin_servers(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(select(XUIServerRecord).order_by(XUIServerRecord.created_at.desc()).limit(50))
    return [
        {
            "id": str(s.id),
            "title": s.name,
            "subtitle": (
                f"{s.health_status} | {'فعال' if s.is_active else 'غیرفعال'} | "
                f"sub: {str((s.metadata_ or {}).get('subscription_scheme') or ('https' if s.base_url.lower().startswith('https://') else 'http'))} | "
                f"priority {s.priority}"
            ),
            "actions": [
                {"label": "فعال/غیرفعال", "action": "toggle_server"},
                {"label": "ساب HTTP", "action": "set_sub_http"},
                {"label": "ساب HTTPS", "action": "set_sub_https"},
                {"label": "از پنل", "action": "set_sub_panel"},
            ],
        }
        for s in result.scalars().all()
    ]


async def _admin_tickets(session: AsyncSession) -> list[dict[str, Any]]:
    tickets = await TicketRepository(session).list_open_tickets(limit=30)
    return [
        {
            "id": str(t.id),
            "title": f"تیکت {str(t.id)[:8]}",
            "subtitle": f"{t.status} | {t.user.telegram_id if t.user else '-'} | {(t.messages[-1].text if t.messages else '') or ''}",
            "actions": [
                {"label": "مشاهده/پاسخ", "action": "view_ticket"},
                {"label": "بستن", "action": "close_ticket"},
            ],
        }
        for t in tickets
    ]


async def _admin_discounts(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(select(DiscountCode).order_by(DiscountCode.created_at.desc()).limit(50))
    return [
        {
            "id": str(d.id),
            "title": d.code,
            "subtitle": f"{d.discount_percent}% | {d.used_count}/{d.max_uses} | {'فعال' if d.is_active else 'غیرفعال'}",
            "actions": [{"label": "فعال/غیرفعال", "action": "toggle_discount"}],
        }
        for d in result.scalars().all()
    ]


async def _admin_settings(session: AsyncSession) -> list[dict[str, Any]]:
    repo = AppSettingsRepository(session)
    trial = await repo.get_trial_settings()
    gw = await repo.get_gateway_settings()
    toman = await repo.get_toman_rate()
    custom = await repo.get_custom_purchase_settings()
    return [
        {"id": "trial", "title": "کانفیگ تست", "subtitle": "فعال" if trial.enabled else "غیرفعال", "actions": []},
        {
            "id": "custom_purchase",
            "title": "خرید حجم و زمان دلخواه",
            "subtitle": (
                f"{'فعال' if custom.enabled else 'غیرفعال'} | "
                f"هر GB: {custom.price_per_gb}$ | هر روز: {custom.price_per_day}$"
            ),
            "actions": [
                {"label": "فعال/غیرفعال", "action": "toggle_custom_purchase"},
                {"label": "قیمت هر GB", "action": "edit_custom_gb"},
                {"label": "قیمت هر روز", "action": "edit_custom_day"},
            ],
        },
        {"id": "tetrapay", "title": "درگاه تتراپی", "subtitle": "فعال" if gw.tetrapay_enabled else "غیرفعال", "actions": []},
        {"id": "tronado", "title": "درگاه ترونادو", "subtitle": "فعال" if gw.tronado_enabled else "غیرفعال", "actions": []},
        {"id": "nowpayments", "title": "NOWPayments", "subtitle": "فعال" if gw.nowpayments_enabled else "غیرفعال", "actions": []},
        {"id": "manual", "title": "پرداخت دستی", "subtitle": "فعال" if gw.manual_crypto_enabled else "غیرفعال", "actions": []},
        {"id": "rate", "title": "نرخ دلار/تومان", "subtitle": str(toman), "actions": []},
    ]


async def _admin_audit_logs(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(AuditLog)
        .options(selectinload(AuditLog.actor))
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    items = []
    for log in result.scalars().unique().all():
        actor = log.actor
        actor_label = actor.first_name or actor.username or str(actor.telegram_id) if actor else "system"
        items.append(
            {
                "id": str(log.id),
                "title": f"{log.action} روی {log.entity_type}",
                "subtitle": f"{actor_label} | {log.created_at:%Y-%m-%d %H:%M}",
                "actions": [],
            }
        )
    return items


# ─── Plans ───────────────────────────────────────────────────────────────────

@router.get("/plans", response_model=PlanListResponse)
async def get_plans(
    session: AsyncSession = Depends(get_db_session),
) -> PlanListResponse:
    result = await session.execute(
        select(Plan)
        .where(Plan.is_active.is_(True), not_(Plan.code.like("custom\\_%", escape="\\")))
        .order_by(Plan.price.asc())
    )
    plans = list(result.scalars().all())
    stock_by_plan_id = await get_effective_plan_stock_map(session, [p.id for p in plans])
    plans = [p for p in plans if is_stock_available(stock_by_plan_id[p.id])]
    settings_repo = AppSettingsRepository(session)
    custom_settings = await settings_repo.get_custom_purchase_settings()
    custom_template = await get_custom_purchase_template_plan(session)
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
                sales_limit=stock_by_plan_id[p.id].sales_limit,
                stock_remaining=stock_by_plan_id[p.id].stock_remaining,
                is_unlimited=stock_by_plan_id[p.id].is_unlimited,
            )
            for p in plans
        ],
        custom_purchase=CustomPurchaseView(
            enabled=custom_settings.enabled,
            price_per_gb=Decimal(str(custom_settings.price_per_gb)),
            price_per_day=Decimal(str(custom_settings.price_per_day)),
            can_purchase=bool(
                custom_settings.enabled
                and custom_settings.price_per_gb > 0
                and custom_settings.price_per_day > 0
                and custom_template is not None
            ),
        ),
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

    # Phone verification check
    phone_settings = await AppSettingsRepository(session).get_phone_verification_settings()
    if phone_settings.enabled:
        if not get_verified_phone(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="برای خرید، ابتدا شماره موبایل خود را از طریق ربات تایید کنید.",
            )

    duplicate_config = await session.scalar(
        select(XUIClientRecord).where(XUIClientRecord.username == config_name)
    )
    if duplicate_config is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="این نام کانفیگ قبلاً استفاده شده است.",
        )

    if user.wallet is None:
        raise HTTPException(status_code=404, detail="کیف پول کاربر پیدا نشد.")

    if body.plan_id is None:
        if payment_method == "wallet":
            plan = await _create_custom_purchase_plan_for_request(session, body)
            return await _purchase_with_wallet(session, user, plan, config_name)
        draft = await _build_custom_purchase_gateway_draft(session, body, config_name)
        if payment_method == "nowpayments":
            return await _create_nowpayments_custom_purchase(session, user, draft)
        if payment_method == "tetrapay":
            return await _create_tetrapay_custom_purchase(session, user, draft)
        if payment_method == "tronado":
            return await _create_tronado_custom_purchase(session, user, draft)
        raise HTTPException(status_code=400, detail="روش پرداخت نامعتبر است.")

    plan = await session.get(Plan, body.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="این پلن در دسترس نیست.")
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError as exc:
        raise HTTPException(status_code=409, detail="موجودی این پلن تمام شده است.") from exc

    if payment_method == "wallet":
        return await _purchase_with_wallet(session, user, plan, config_name)
    if payment_method == "nowpayments":
        return await _create_nowpayments_purchase(session, user, plan, config_name)
    if payment_method == "tetrapay":
        return await _create_tetrapay_purchase(session, user, plan, config_name)
    if payment_method == "tronado":
        return await _create_tronado_purchase(session, user, plan, config_name)

    raise HTTPException(status_code=400, detail="روش پرداخت نامعتبر است.")


async def _create_custom_purchase_plan_for_request(
    session: AsyncSession,
    body: PurchaseRequest,
) -> Plan:
    try:
        volume_gb = float(body.custom_volume_gb or 0)
        duration_days = int(body.custom_duration_days or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="حجم یا مدت خرید دلخواه معتبر نیست.") from exc
    settings_repo = AppSettingsRepository(session)
    custom_settings = await settings_repo.get_custom_purchase_settings()
    template_plan = await get_custom_purchase_template_plan(session)
    if template_plan is None:
        raise HTTPException(status_code=400, detail="برای خرید دلخواه حداقل یک پلن فعال متصل به سرور لازم است.")
    try:
        price = calculate_custom_purchase_price(
            custom_settings,
            volume_gb=volume_gb,
            duration_days=duration_days,
        )
        return await create_custom_purchase_plan(
            session,
            volume_gb=volume_gb,
            duration_days=duration_days,
            price=price,
            template_plan=template_plan,
        )
    except CustomPurchaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _build_custom_purchase_gateway_draft(
    session: AsyncSession,
    body: PurchaseRequest,
    config_name: str,
) -> dict[str, Any]:
    try:
        volume_gb = float(body.custom_volume_gb or 0)
        duration_days = int(body.custom_duration_days or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="حجم یا مدت خرید دلخواه معتبر نیست.") from exc

    settings_repo = AppSettingsRepository(session)
    custom_settings = await settings_repo.get_custom_purchase_settings()
    template_plan = await get_custom_purchase_template_plan(session)
    if template_plan is None:
        raise HTTPException(status_code=400, detail="برای خرید دلخواه حداقل یک پلن فعال متصل به سرور لازم است.")
    try:
        price = calculate_custom_purchase_price(
            custom_settings,
            volume_gb=volume_gb,
            duration_days=duration_days,
        )
    except CustomPurchaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "price": price,
        "name": f"Custom {volume_gb:g}GB / {duration_days} days",
        "callback_payload": {
            "custom_purchase": True,
            "custom_volume_gb": volume_gb,
            "custom_duration_days": duration_days,
            "config_name": config_name,
            "discount_percent": 0,
            "discount_id": None,
            "purpose": "direct_purchase",
            "source": "miniapp",
        },
    }


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


async def _create_nowpayments_custom_purchase(
    session: AsyncSession,
    user: User,
    draft: dict[str, Any],
) -> PurchaseResponse:
    from repositories.settings import AppSettingsRepository

    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.nowpayments_enabled:
        raise HTTPException(status_code=400, detail="درگاه NOWPayments غیرفعال است.")

    price = Decimal(str(draft["price"]))
    api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key
    local_order_id = str(uuid4())
    payload = NowPaymentsPaymentCreateRequest(
        price_amount=price,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Custom purchase for user {user.id}",
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
        price_amount=price,
        invoice_url=str(invoice.invoice_url),
        callback_payload=dict(draft["callback_payload"]),
    )
    session.add(payment)
    await session.flush()

    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت خرید دلخواه ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
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


async def _create_tetrapay_custom_purchase(
    session: AsyncSession,
    user: User,
    draft: dict[str, Any],
) -> PurchaseResponse:
    from repositories.settings import AppSettingsRepository

    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    if not gw.tetrapay_enabled:
        raise HTTPException(status_code=400, detail="درگاه تتراپی غیرفعال است.")

    price = Decimal(str(draft["price"]))
    toman_rate = await settings_repo.get_toman_rate()
    toman_amount = int((price * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10
    if rial_amount < 10000:
        raise HTTPException(status_code=400, detail="مبلغ این خرید کمتر از حداقل مجاز درگاه تتراپی است.")

    local_order_id = str(uuid4())
    api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()

    try:
        async with TetraPayClient(
            TetraPayClientConfig(api_key=api_key, base_url=settings.tetrapay_base_url)
        ) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"خرید دلخواه - کاربر {user.telegram_id}",
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
        price_amount=price,
        pay_amount=toman_amount,
        invoice_url=tx.payment_url_bot,
        callback_payload=dict(draft["callback_payload"]),
    )
    session.add(payment)
    await session.flush()

    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت خرید دلخواه ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
        payment_method="tetrapay",
        invoice_url=tx.payment_url_bot,
        payment_id=payment.id,
    )


async def _create_tronado_purchase(
    session: AsyncSession,
    user: User,
    plan: Plan,
    config_name: str,
) -> PurchaseResponse:
    invoice = await create_tronado_invoice(
        session=session,
        user=user,
        amount_usd=plan.price,
        kind="direct_purchase",
        description=f"Purchase plan {plan.name} for user {user.id}",
        callback_payload={
            "plan_id": str(plan.id),
            "config_name": config_name,
            "discount_percent": 0,
            "discount_id": None,
            "purpose": "direct_purchase",
            "source": "miniapp",
        },
    )
    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت ترونادو ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
        payment_method="tronado",
        invoice_url=invoice.invoice_url,
        payment_id=invoice.payment.id,
    )


# ─── Renewal ─────────────────────────────────────────────────────────────────

async def _create_tronado_custom_purchase(
    session: AsyncSession,
    user: User,
    draft: dict[str, Any],
) -> PurchaseResponse:
    invoice = await create_tronado_invoice(
        session=session,
        user=user,
        amount_usd=Decimal(str(draft["price"])),
        kind="direct_purchase",
        description=f"Custom purchase for user {user.id}",
        callback_payload=dict(draft["callback_payload"]),
    )
    return PurchaseResponse(
        status="invoice_created",
        message="فاکتور پرداخت خرید دلخواه ترونادو ساخته شد. بعد از پرداخت، کانفیگ خودکار ساخته می‌شود.",
        payment_method="tronado",
        invoice_url=invoice.invoice_url,
        payment_id=invoice.payment.id,
    )


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
    subscription = await _get_user_subscription(session, user, body.subscription_id)
    _validate_renewal_request(subscription, body.renew_type, body.amount)

    renewal_settings = await AppSettingsRepository(session).get_renewal_settings()
    price = calculate_renewal_price(
        renew_type=body.renew_type,
        amount=body.amount,
        settings=renewal_settings,
    )

    renewal_meta = {
        "sub_id": str(subscription.id),
        "renew_type": body.renew_type,
        "renew_amount": body.amount,
        "purpose": "renewal",
        "source": "miniapp",
    }

    # ── Gateway renewals ──
    if body.payment_method in ("nowpayments", "tetrapay", "tronado"):
        gw = await AppSettingsRepository(session).get_gateway_settings()
        local_order_id = str(uuid4())

        if body.payment_method == "nowpayments":
            if not gw.nowpayments_enabled:
                raise HTTPException(status_code=400, detail="درگاه ارزی فعال نیست.")
            api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key
            try:
                async with NowPaymentsClient(
                    NowPaymentsClientConfig(
                        api_key=api_key,
                        base_url=settings.nowpayments_base_url,
                    )
                ) as client:
                    inv = await client.create_payment_invoice(
                        NowPaymentsPaymentCreateRequest(
                            price_amount=float(price),
                            price_currency="usd",
                            order_id=local_order_id,
                            order_description=f"Renewal sub {subscription.id}",
                            ipn_callback_url=settings.nowpayments_ipn_callback_url,
                        )
                    )
            except NowPaymentsRequestError as exc:
                raise HTTPException(status_code=502, detail=f"خطا در ساخت فاکتور: {exc}") from exc

            payment = Payment(
                user_id=user.id,
                provider="nowpayments",
                kind="direct_renewal",
                provider_invoice_id=str(inv.id),
                order_id=local_order_id,
                payment_status="waiting",
                price_currency="USD",
                price_amount=price,
                invoice_url=str(inv.invoice_url),
                callback_payload=renewal_meta,
            )
            session.add(payment)
            await session.flush()
            return RenewalResponse(
                status="invoice_created",
                message="فاکتور تمدید ساخته شد. بعد از پرداخت، تمدید خودکار اعمال می‌شود.",
                price=price,
                invoice_url=str(inv.invoice_url),
            )

        elif body.payment_method == "tetrapay":
            if not gw.tetrapay_enabled:
                raise HTTPException(status_code=400, detail="درگاه ریالی فعال نیست.")
            toman_rate = await AppSettingsRepository(session).get_toman_rate()
            toman_amount = int(float(price) * toman_rate)
            rial_amount = toman_amount * 10

            try:
                async with TetraPayClient(
                    TetraPayClientConfig(
                        api_key=settings.tetrapay_api_key.get_secret_value(),
                        base_url=settings.tetrapay_base_url,
                    )
                ) as client:
                    tx = await client.create_order(
                        hash_id=local_order_id,
                        amount=rial_amount,
                        description=f"تمدید سرویس - {user.telegram_id}",
                        email=f"{user.telegram_id}@telegram.org",
                        mobile="09111111111",
                    )
            except TetraPayRequestError as exc:
                raise HTTPException(status_code=502, detail=f"خطا در ساخت فاکتور: {exc}") from exc

            payment = Payment(
                user_id=user.id,
                provider="tetrapay",
                kind="direct_renewal",
                provider_payment_id=tx.Authority,
                order_id=local_order_id,
                payment_status="waiting",
                pay_currency="IRT",
                price_currency="USD",
                price_amount=price,
                pay_amount=toman_amount,
                invoice_url=tx.payment_url_bot,
                callback_payload=renewal_meta,
            )
            session.add(payment)
            await session.flush()
            return RenewalResponse(
                status="invoice_created",
                message=f"فاکتور تمدید ریالی: {toman_amount:,} تومان. بعد از پرداخت، تمدید خودکار اعمال می‌شود.",
                price=price,
                invoice_url=tx.payment_url_bot,
            )

        elif body.payment_method == "tronado":
            invoice = await create_tronado_invoice(
                session=session,
                user=user,
                amount_usd=price,
                kind="direct_renewal",
                description=f"Renewal sub {subscription.id}",
                callback_payload=renewal_meta,
            )
            return RenewalResponse(
                status="invoice_created",
                message="فاکتور تمدید ترونادو ساخته شد. بعد از پرداخت، تمدید خودکار اعمال می‌شود.",
                price=price,
                invoice_url=invoice.invoice_url,
            )

    # ── Wallet renewal (default) ──
    if body.payment_method != "wallet":
        raise HTTPException(status_code=400, detail="روش پرداخت نامعتبر.")

    if user.wallet is None:
        raise HTTPException(status_code=404, detail="کیف پول پیدا نشد.")
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


@router.get("/payments", response_model=PaymentListResponse)
async def get_payments(
    page: int = 1,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> PaymentListResponse:
    user, session = auth
    page_size = 20
    offset = (max(page, 1) - 1) * page_size

    total = await session.scalar(
        select(func.count()).select_from(Payment).where(Payment.user_id == user.id)
    ) or 0
    result = await session.execute(
        select(Payment)
        .where(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    payments = list(result.scalars().all())
    return PaymentListResponse(payments=[PaymentView.model_validate(p) for p in payments], total=total)


@router.post("/wallet/topup", response_model=TopUpResponse)
async def create_wallet_topup(
    body: TopUpRequest,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> TopUpResponse:
    user, session = auth
    amount = body.amount.quantize(Decimal("0.01"))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="مبلغ شارژ باید بیشتر از صفر باشد.")
    if body.payment_method == "nowpayments":
        return await _create_nowpayments_topup(session, user, amount)
    if body.payment_method == "tetrapay":
        return await _create_tetrapay_topup(session, user, amount)
    if body.payment_method == "tronado":
        return await _create_tronado_topup(session, user, amount)
    raise HTTPException(status_code=400, detail="روش شارژ نامعتبر است.")


@router.post("/payments/{payment_id}/refresh")
async def refresh_payment(
    payment_id: UUID,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id, Payment.user_id == user.id)
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="پرداخت پیدا نشد.")
    if payment.provider not in {"nowpayments", "tetrapay", "tronado"}:
        raise HTTPException(status_code=400, detail="این پرداخت قابل بازبینی خودکار نیست.")
    result = await review_gateway_payment(session, payment)
    await session.refresh(payment)
    return {
        "ok": True,
        "message": f"وضعیت پرداخت بررسی شد: {result}",
        "payment": PaymentView.model_validate(payment),
    }


async def _create_nowpayments_topup(
    session: AsyncSession,
    user: User,
    amount: Decimal,
) -> TopUpResponse:
    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.nowpayments_enabled:
        raise HTTPException(status_code=400, detail="درگاه NOWPayments غیرفعال است.")

    api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key
    local_order_id = str(uuid4())
    payload = NowPaymentsPaymentCreateRequest(
        price_amount=amount,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Wallet top-up for user {user.id}",
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
        kind="wallet_topup",
        provider_payment_id=None,
        provider_invoice_id=str(invoice.id),
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency=None,
        price_currency="USD",
        price_amount=amount,
        invoice_url=str(invoice.invoice_url),
        callback_payload={"source": "miniapp"},
    )
    session.add(payment)
    await session.flush()
    return TopUpResponse(
        status="invoice_created",
        message="فاکتور شارژ ساخته شد. بعد از پرداخت، موجودی کیف پول خودکار بروزرسانی می‌شود.",
        payment_method="nowpayments",
        invoice_url=str(invoice.invoice_url),
        payment_id=payment.id,
    )


async def _create_tetrapay_topup(
    session: AsyncSession,
    user: User,
    amount: Decimal,
) -> TopUpResponse:
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    if not gw.tetrapay_enabled:
        raise HTTPException(status_code=400, detail="درگاه تتراپی غیرفعال است.")

    toman_rate = await settings_repo.get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        raise HTTPException(status_code=400, detail="نرخ تبدیل تومان تنظیم نشده است.")
    toman_amount = int((amount * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10
    if rial_amount < 10000:
        raise HTTPException(status_code=400, detail="مبلغ کمتر از حداقل مجاز درگاه تتراپی است.")
    if toman_amount > settings.tetrapay_max_amount_toman:
        raise HTTPException(status_code=400, detail="مبلغ بیشتر از سقف مجاز درگاه تتراپی است.")

    local_order_id = str(uuid4())
    api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()
    try:
        async with TetraPayClient(
            TetraPayClientConfig(api_key=api_key, base_url=settings.tetrapay_base_url)
        ) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"شارژ کیف پول - کاربر {user.telegram_id}",
                email=f"{user.telegram_id}@telegram.org",
                mobile="09111111111",
            )
    except TetraPayRequestError as exc:
        raise HTTPException(status_code=502, detail="خطا در ساخت فاکتور تتراپی.") from exc

    invoice_url = tx.payment_url_web or tx.payment_url_bot
    payment = Payment(
        user_id=user.id,
        provider="tetrapay",
        kind="wallet_topup",
        provider_payment_id=tx.Authority,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=amount,
        pay_amount=toman_amount,
        invoice_url=invoice_url,
        callback_payload={"source": "miniapp"},
    )
    session.add(payment)
    await session.flush()
    return TopUpResponse(
        status="invoice_created",
        message="فاکتور شارژ ساخته شد. بعد از پرداخت، موجودی کیف پول خودکار بروزرسانی می‌شود.",
        payment_method="tetrapay",
        invoice_url=invoice_url,
        payment_id=payment.id,
        pay_amount=Decimal(toman_amount),
        pay_currency="IRT",
    )


async def _create_tronado_topup(
    session: AsyncSession,
    user: User,
    amount: Decimal,
) -> TopUpResponse:
    invoice = await create_tronado_invoice(
        session=session,
        user=user,
        amount_usd=amount,
        kind="wallet_topup",
        description=f"Wallet top-up for user {user.id}",
        callback_payload={"source": "miniapp"},
    )
    return TopUpResponse(
        status="invoice_created",
        message="فاکتور شارژ ترونادو ساخته شد. بعد از پرداخت، موجودی کیف پول خودکار بروزرسانی می‌شود.",
        payment_method="tronado",
        invoice_url=invoice.invoice_url,
        payment_id=invoice.payment.id,
        pay_amount=invoice.tron_amount,
        pay_currency="TRX",
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
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="متن پیام خالی است.")
    repo = TicketRepository(session)
    ticket = await repo.get_open_ticket_for_user(user.id)

    if ticket is None:
        ticket = await repo.create_ticket(user_id=user.id, status="open")

    if ticket.status == "answered":
        ticket.status = "open"

    await repo.add_message(
        ticket_id=ticket.id,
        sender_id=user.id,
        text=text,
    )
    return {"ok": True, "ticket_id": str(ticket.id)}


@router.post("/tickets/{ticket_id}/close")
async def close_own_ticket(
    ticket_id: UUID,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    user, session = auth
    ticket = await session.scalar(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == user.id)
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="تیکت پیدا نشد.")
    ticket.status = "closed"
    await session.flush()
    return {"ok": True, "message": "تیکت بسته شد."}


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

    if (datetime.now(timezone.utc) - auth_date).total_seconds() > 4 * 60 * 60:
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
