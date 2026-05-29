"""
Dashboard user-management endpoints:

    GET    /api/dashboard/users                — paginated list + search
    GET    /api/dashboard/users/{id}           — full profile + subs + txns
    PATCH  /api/dashboard/users/{id}/balance   — credit / debit wallet
    PATCH  /api/dashboard/users/{id}/credit    — set credit limit
    PATCH  /api/dashboard/users/{id}/status    — active | banned
    POST   /api/dashboard/users/{id}/message   — send a Telegram DM via the bot

Every mutating endpoint records an AuditLog row (same shape the bot
itself uses) so the operator's actions are traceable.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from models.subscription import Subscription
from models.user import User
from models.wallet import Wallet, WalletTransaction
from repositories.audit import AuditLogRepository
from services.wallet.manager import WalletManager


logger = logging.getLogger(__name__)
router = APIRouter()


AuthDep = Annotated[
    tuple[DashboardAdmin, AsyncSession],
    Depends(require_dashboard_admin),
]


# ─── List + search ───────────────────────────────────────────────────────


@router.get("")
async def list_users(
    auth: AuthDep,
    q: str = Query("", description="Free-text search across telegram_id / username / first_name / phone."),
    status: str | None = Query(None, description='"active" | "banned" | omit for all'),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("created_at", description="Field to sort by"),
    order: str = Query("desc", regex="^(asc|desc)$"),
) -> dict[str, Any]:
    """One round-trip: filtered count + page of rows + wallet snapshot."""
    _admin, session = auth

    stmt = select(User).options(selectinload(User.wallet))
    if q:
        q_stripped = q.strip()
        like = f"%{q_stripped}%"
        # Telegram IDs are pure digits — match exactly when the search
        # term parses as int.
        try:
            tg_int = int(q_stripped)
        except ValueError:
            tg_int = None
        conditions = [
            User.username.ilike(like),
            User.first_name.ilike(like),
        ]
        if tg_int is not None:
            conditions.append(User.telegram_id == tg_int)
        stmt = stmt.where(or_(*conditions))

    if status in ("active", "banned"):
        stmt = stmt.where(User.status == status)

    # Sort
    sort_col_map = {
        "created_at": User.created_at,
        "last_seen_at": User.last_seen_at,
        "telegram_id": User.telegram_id,
    }
    sort_col = sort_col_map.get(sort, User.created_at)
    stmt = stmt.order_by(desc(sort_col) if order == "desc" else sort_col.asc())

    # Count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(await session.scalar(count_stmt) or 0)

    # Page
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    for u in rows:
        items.append({
            "id": str(u.id),
            "telegram_id": int(u.telegram_id),
            "username": u.username,
            "first_name": u.first_name,
            "role": u.role,
            "status": u.status,
            "balance_usd": float(Decimal(str(u.wallet.balance))) if u.wallet else 0.0,
            "credit_limit_usd": float(Decimal(str(u.wallet.credit_limit))) if (u.wallet and u.wallet.credit_limit is not None) else 0.0,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


# ─── Detail ──────────────────────────────────────────────────────────────


@router.get("/{user_id}")
async def user_detail(user_id: UUID, auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth

    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet))
        .where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")

    # Subscriptions (most recent 20)
    subs_rows = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.xui_client))
        .where(Subscription.user_id == user.id)
        .order_by(desc(Subscription.created_at))
        .limit(20)
    )).scalars().all()

    subs = []
    for s in subs_rows:
        name: str
        if s.source == "imported_legacy" and s.legacy_remark:
            name = s.legacy_remark
        elif s.xui_client:
            name = s.xui_client.username or ""
        else:
            name = ""
        subs.append({
            "id": str(s.id),
            "status": s.status,
            "source": s.source,
            "name": name,
            "volume_bytes": int(s.volume_bytes or 0),
            "used_bytes": int(s.used_bytes or 0),
            "lifetime_used_bytes": int(s.lifetime_used_bytes or 0),
            "starts_at": s.starts_at.isoformat() if s.starts_at else None,
            "ends_at": s.ends_at.isoformat() if s.ends_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    # Wallet transactions (most recent 15)
    txns_rows: list[WalletTransaction] = []
    if user.wallet:
        txns_rows = list((await session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == user.wallet.id)
            .order_by(desc(WalletTransaction.created_at))
            .limit(15)
        )).scalars().all())
    txns = [{
        "id": str(t.id),
        "type": t.type,
        "direction": t.direction,
        "amount": float(Decimal(str(t.amount or 0))),
        "currency": t.currency,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "balance_after": float(Decimal(str(t.balance_after or 0))) if t.balance_after is not None else None,
    } for t in txns_rows]

    return {
        "user": {
            "id": str(user.id),
            "telegram_id": int(user.telegram_id),
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "language_code": user.language_code,
            "role": user.role,
            "status": user.status,
            "is_bot_blocked": user.is_bot_blocked,
            "has_received_free_trial": user.has_received_free_trial,
            "ref_code": user.ref_code,
            "personal_discount_percent": user.personal_discount_percent,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
            "balance_usd": float(Decimal(str(user.wallet.balance))) if user.wallet else 0.0,
            "credit_limit_usd": float(Decimal(str(user.wallet.credit_limit))) if (user.wallet and user.wallet.credit_limit is not None) else 0.0,
        },
        "subscriptions": subs,
        "wallet_transactions": txns,
    }


# ─── Mutations ───────────────────────────────────────────────────────────


class BalanceAdjustBody(BaseModel):
    amount: float = Field(..., description="USD amount; positive=credit, negative=debit")
    description: str = Field("Adjusted from dashboard", max_length=200)


@router.patch("/{user_id}/balance")
async def adjust_balance(
    user_id: UUID,
    body: BalanceAdjustBody,
    auth: AuthDep,
) -> dict[str, Any]:
    admin, session = auth
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")
    if body.amount == 0:
        raise HTTPException(status_code=400, detail="مقدار نمی‌تواند صفر باشد.")

    wallet_mgr = WalletManager(session)
    amount_decimal = Decimal(str(abs(body.amount)))
    direction = "credit" if body.amount > 0 else "debit"
    tx_type = "deposit" if body.amount > 0 else "refund"
    try:
        await wallet_mgr.process_transaction(
            user_id=user.id,
            amount=amount_decimal,
            transaction_type=tx_type,
            direction=direction,
            currency="USD",
            reference_type="dashboard_admin_action",
            reference_id=admin.id,
            description=body.description[:200],
            metadata={"dashboard_admin": admin.username},
        )
    except Exception as exc:
        logger.error("Dashboard balance-adjust failed for user %s: %s", user_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"خطای کیف پول: {exc}")

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,  # No User-row actor for dashboard admins
            action="dashboard_balance_adjust",
            entity_type="user",
            entity_id=user.id,
            payload={
                "dashboard_admin": admin.username,
                "amount_usd": float(body.amount),
                "description": body.description,
            },
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()

    new_user = await session.scalar(
        select(User).options(selectinload(User.wallet)).where(User.id == user.id)
    )
    return {
        "ok": True,
        "balance_usd": float(Decimal(str(new_user.wallet.balance))) if (new_user and new_user.wallet) else 0.0,
    }


class CreditLimitBody(BaseModel):
    credit_limit: float = Field(..., ge=0)


@router.patch("/{user_id}/credit")
async def set_credit_limit(user_id: UUID, body: CreditLimitBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    user = await session.scalar(
        select(User).options(selectinload(User.wallet)).where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")
    if user.wallet is None:
        # Create the wallet so we can set credit on it.
        wallet = Wallet(user_id=user.id, balance=Decimal("0.00"))
        session.add(wallet)
        await session.flush()
        user.wallet = wallet

    user.wallet.credit_limit = Decimal(str(body.credit_limit))
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_credit_limit",
            entity_type="user",
            entity_id=user.id,
            payload={"dashboard_admin": admin.username, "credit_limit": float(body.credit_limit)},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "credit_limit_usd": float(body.credit_limit)}


class StatusBody(BaseModel):
    status: str = Field(..., pattern="^(active|banned)$")


@router.patch("/{user_id}/status")
async def set_status(user_id: UUID, body: StatusBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")
    user.status = body.status
    disabled_configs = 0
    if body.status == "banned":
        # Cut the user's VPN too, not just their DB status (consistent with the
        # bot + mini-app ban paths).
        user.is_bot_blocked = True
        from services.provisioning.manager import ProvisioningManager
        try:
            disabled_configs = await ProvisioningManager(session).disable_user_active_configs(user.id)
        except Exception as exc:
            logger.warning("set_status: failed to disable configs for user %s: %s", user.id, exc)
    else:
        user.is_bot_blocked = False
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action=f"dashboard_user_{body.status}",
            entity_type="user",
            entity_id=user.id,
            payload={"dashboard_admin": admin.username, "disabled_configs": disabled_configs},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "status": user.status, "disabled_configs": disabled_configs}


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/{user_id}/message")
async def send_message(user_id: UUID, body: MessageBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")
    if user.is_bot_blocked:
        raise HTTPException(status_code=400, detail="کاربر ربات را block کرده است؛ ارسال پیام ممکن نیست.")

    # Use a short-lived bot session for the outbound DM. Reusing
    # `_get_shared_bot()` from services.payment would force-import that
    # module just for the side effect; instead spin up our own minimal
    # client here so this endpoint stays self-contained.
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from core.config import settings

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
    )
    try:
        await bot.send_message(user.telegram_id, body.text)
    except TelegramForbiddenError:
        user.is_bot_blocked = True
        await session.commit()
        raise HTTPException(status_code=400, detail="کاربر ربات را مسدود کرده.")
    except TelegramBadRequest as exc:
        raise HTTPException(status_code=400, detail=f"خطای Telegram: {exc}")
    except Exception as exc:
        logger.error("Dashboard DM failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        await bot.session.close()

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_user_message",
            entity_type="user",
            entity_id=user.id,
            payload={"dashboard_admin": admin.username, "preview": body.text[:120]},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}
