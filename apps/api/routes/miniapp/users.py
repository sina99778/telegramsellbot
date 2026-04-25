"""
Full Mini App API endpoints.
Provides dashboard, plans, configs, wallet, tickets, and referral data.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.dependencies.db import get_db_session
from core.config import settings
from models.plan import Plan
from models.subscription import Subscription
from models.ticket import Ticket, TicketMessage
from models.user import User
from models.wallet import WalletTransaction
from repositories.ticket import TicketRepository
from schemas.api.miniapp import (
    MiniAppDashboardResponse,
    PlanListResponse,
    PlanView,
    ReferralView,
    SendTicketRequest,
    SubscriptionView,
    TicketListResponse,
    TicketMessageView,
    TicketView,
    TransactionListResponse,
    TransactionView,
    WalletView,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Auth helper ─────────────────────────────────────────────────────────────

async def _get_current_user(
    init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db_session),
) -> tuple[User, AsyncSession]:
    if not init_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="لطفاً از داخل ربات تلگرام وارد شوید. (initData is empty)",
        )
    telegram_user_id = validate_telegram_init_data(init_data)
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
        wallet=WalletView.model_validate(user.wallet),
        subscriptions=subs,
        active_config_count=active_count,
        total_volume_used=total_used,
        total_volume=total_vol,
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
