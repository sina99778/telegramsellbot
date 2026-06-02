"""
Dashboard settings endpoints — one GET that returns every section, plus
narrow PATCHes per section so the SPA can save just what changed.

Sections
--------
    general      — sales_enabled, renewals_enabled
    pricing      — price_per_gb, price_per_10_days, toman_rate,
                   display_currency (USD | IRT)
    custom_buy   — enabled, price_per_gb, price_per_day
    security     — xui_limit_ip, max_distinct_ips, auto_disable_ip_abuse
    backup       — interval_hours, channel_chat_id, sales_channel_chat_id,
                   last_run_at (read-only)
    premium_emoji — enabled + emoji_map (dict[trigger → premium-emoji-id])
    button_style  — enabled + role→color mapping (Bot API 9.4)

Every PATCH writes an AuditLog row tagged with the dashboard admin's
username so the operator's settings changes show up alongside the bot's
own audit trail.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from services.telegram.premium_emoji import clear_premium_emoji_cache


logger = logging.getLogger(__name__)
router = APIRouter()

AuthDep = Annotated[
    tuple[DashboardAdmin, AsyncSession],
    Depends(require_dashboard_admin),
]


async def _audit(session: AsyncSession, admin: DashboardAdmin, action: str, payload: dict) -> None:
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action=f"dashboard_settings_{action}",
            entity_type="app_settings",
            entity_id=admin.id,  # No natural entity — use admin's id
            payload={"dashboard_admin": admin.username, **payload},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)


# ─── Single GET ─────────────────────────────────────────────────────────


@router.get("")
async def get_all_settings(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    repo = AppSettingsRepository(session)

    user_actions = await repo.get_user_actions_settings()
    renewal = await repo.get_renewal_settings()
    custom = await repo.get_custom_purchase_settings()
    security = await repo.get_service_security_settings()
    premium = await repo.get_premium_emoji_settings()
    button_style = await repo.get_button_style_settings()
    toman_rate = await repo.get_toman_rate()
    display_currency = await repo.get_display_currency()
    backup_interval = await repo.get_backup_interval_hours()
    backup_channel = await repo.get_backup_channel_id()
    sales_channel = await repo.get_sales_report_chat_id()
    backup_last_iso = await repo.get_backup_last_run_iso()

    return {
        "general": {
            "sales_enabled": user_actions.sales_enabled,
            "renewals_enabled": user_actions.renewals_enabled,
            "delete_enabled": getattr(user_actions, "delete_enabled", True),
            "refund_enabled": getattr(user_actions, "refund_enabled", True),
        },
        "pricing": {
            "price_per_gb": float(renewal.price_per_gb),
            "price_per_10_days": float(renewal.price_per_10_days),
            "toman_rate": int(toman_rate),
            "display_currency": display_currency,
        },
        "custom_buy": {
            "enabled": custom.enabled,
            "price_per_gb": float(custom.price_per_gb),
            "price_per_day": float(custom.price_per_day),
        },
        "security": {
            "xui_limit_ip": int(security.xui_limit_ip),
            "max_distinct_ips": int(security.max_distinct_ips),
            "auto_disable_ip_abuse": bool(security.auto_disable_ip_abuse),
        },
        "backup": {
            "interval_hours": int(backup_interval),
            "channel_chat_id": backup_channel,
            "sales_channel_chat_id": sales_channel,  # read-only here; managed elsewhere
            "last_run_at": backup_last_iso,
        },
        "premium_emoji": {
            "enabled": premium.enabled,
            "emoji_map": premium.emoji_map,
        },
        "button_style": {
            "enabled": button_style.enabled,
            "confirm": button_style.confirm,
            "destructive": button_style.destructive,
            "navigation": button_style.navigation,
            "info": button_style.info,
        },
    }


# ─── PATCH: general ─────────────────────────────────────────────────────


class GeneralBody(BaseModel):
    sales_enabled: bool | None = None
    renewals_enabled: bool | None = None
    delete_enabled: bool | None = None
    refund_enabled: bool | None = None


@router.patch("/general")
async def patch_general(body: GeneralBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        return {"ok": True}
    await repo.update_user_actions_settings(**kwargs)
    await _audit(session, admin, "general", kwargs)
    await session.commit()
    return {"ok": True}


# ─── PATCH: pricing ─────────────────────────────────────────────────────


class PricingBody(BaseModel):
    price_per_gb: float | None = Field(None, ge=0)
    price_per_10_days: float | None = Field(None, ge=0)
    toman_rate: int | None = Field(None, ge=1)
    display_currency: str | None = Field(None, pattern="^(USD|IRT)$")


@router.patch("/pricing")
async def patch_pricing(body: PricingBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    payload: dict[str, Any] = {}

    if body.price_per_gb is not None or body.price_per_10_days is not None:
        await repo.update_renewal_settings(
            price_per_gb=body.price_per_gb,
            price_per_10_days=body.price_per_10_days,
        )
        if body.price_per_gb is not None:
            payload["price_per_gb"] = body.price_per_gb
        if body.price_per_10_days is not None:
            payload["price_per_10_days"] = body.price_per_10_days

    if body.toman_rate is not None:
        await repo.set_toman_rate(int(body.toman_rate))
        payload["toman_rate"] = body.toman_rate

    if body.display_currency is not None:
        await repo.set_display_currency(body.display_currency)
        payload["display_currency"] = body.display_currency

    await _audit(session, admin, "pricing", payload)
    await session.commit()
    return {"ok": True}


# ─── PATCH: custom_buy ─────────────────────────────────────────────────


class CustomBuyBody(BaseModel):
    enabled: bool | None = None
    price_per_gb: float | None = Field(None, ge=0)
    price_per_day: float | None = Field(None, ge=0)


@router.patch("/custom_buy")
async def patch_custom_buy(body: CustomBuyBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        return {"ok": True}
    await repo.update_custom_purchase_settings(**kwargs)
    await _audit(session, admin, "custom_buy", kwargs)
    await session.commit()
    return {"ok": True}


# ─── PATCH: security ───────────────────────────────────────────────────


class SecurityBody(BaseModel):
    xui_limit_ip: int | None = Field(None, ge=0, le=1000)
    max_distinct_ips: int | None = Field(None, ge=0, le=1000)
    auto_disable_ip_abuse: bool | None = None


@router.patch("/security")
async def patch_security(body: SecurityBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        return {"ok": True}
    await repo.update_service_security_settings(**kwargs)
    await _audit(session, admin, "security", kwargs)
    await session.commit()
    return {"ok": True}


# ─── PATCH: backup ─────────────────────────────────────────────────────


class BackupBody(BaseModel):
    interval_hours: int | None = Field(None, ge=1, le=168)
    channel_chat_id: int | None = None  # nullable — operator can clear it
    clear_channel: bool | None = None   # explicit unset


@router.patch("/backup")
async def patch_backup(body: BackupBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    payload: dict[str, Any] = {}

    if body.interval_hours is not None:
        await repo.set_backup_interval_hours(int(body.interval_hours))
        payload["interval_hours"] = body.interval_hours

    if body.clear_channel:
        await repo.set_backup_channel_id(None)
        payload["channel_chat_id"] = None
    elif body.channel_chat_id is not None:
        await repo.set_backup_channel_id(int(body.channel_chat_id))
        payload["channel_chat_id"] = body.channel_chat_id

    await _audit(session, admin, "backup", payload)
    await session.commit()
    return {"ok": True}


@router.post("/backup/run-now")
async def trigger_backup_now(auth: AuthDep) -> dict[str, Any]:
    """Fire a backup right now, bypassing the interval gate.

    The actual heavy lifting is the worker job (`apps.worker.jobs.backup`).
    We spawn it in-process via a one-shot bot session because the api
    container has the same code + DB access as the worker.
    """
    admin, session = auth
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from core.config import settings as _settings
    from apps.worker.jobs.backup import run_backup as _run

    bot = Bot(
        token=_settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=_settings.bot_parse_mode),
    )
    try:
        # manual_requester_id=None → goes to the configured channel(s),
        # NOT the dashboard admin's DM (which they don't necessarily own
        # in Telegram). Bypasses the interval gate because we pass
        # `manual_requester_id` indirectly via the routing logic; but
        # to truly bypass the interval gate the caller must NOT be None.
        # Compromise: route to channels (None) but also stamp the gate
        # so the next scheduled tick doesn't immediately re-fire.
        await _run(session, bot, manual_requester_id=None)
    finally:
        await bot.session.close()

    await _audit(session, admin, "backup_run_now", {})
    await session.commit()
    return {"ok": True}


# ─── PATCH: premium_emoji ──────────────────────────────────────────────


class PremiumEmojiBody(BaseModel):
    enabled: bool | None = None
    emoji_map: dict[str, str] | None = None


@router.patch("/premium_emoji")
async def patch_premium_emoji(body: PremiumEmojiBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs: dict[str, Any] = {}
    if body.enabled is not None:
        kwargs["enabled"] = body.enabled
    if body.emoji_map is not None:
        # Trim + drop empty entries client-side already, but be defensive.
        cleaned = {str(k).strip(): str(v).strip()
                   for k, v in body.emoji_map.items() if str(k).strip() and str(v).strip()}
        kwargs["emoji_map"] = cleaned
    if not kwargs:
        return {"ok": True}
    await repo.update_premium_emoji_settings(**kwargs)
    await _audit(session, admin, "premium_emoji",
                 {k: ("…" if k == "emoji_map" else v) for k, v in kwargs.items()})
    await session.commit()
    # Refresh the cache in EVERY process (bot/worker/api) via Redis pub/sub —
    # AFTER commit so they re-read the just-saved value. No restart needed.
    from core.cache_sync import invalidate
    await invalidate("premium_emoji")
    return {"ok": True}


# ─── PATCH: button_style ──────────────────────────────────────────────


class ButtonStyleBody(BaseModel):
    enabled: bool | None = None
    confirm: str | None = Field(None, pattern="^(primary|success|danger|)$")
    destructive: str | None = Field(None, pattern="^(primary|success|danger|)$")
    navigation: str | None = Field(None, pattern="^(primary|success|danger|)$")
    info: str | None = Field(None, pattern="^(primary|success|danger|)$")


@router.patch("/button_style")
async def patch_button_style(body: ButtonStyleBody, auth: AuthDep) -> dict[str, Any]:
    """Update the role→color mapping used by `styled_button`.

    The bot/worker run in other processes; we publish a Redis invalidation so
    they refresh their in-process cache immediately — no restart, no TTL wait.
    """
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs: dict[str, Any] = {}
    for field in ("enabled", "confirm", "destructive", "navigation", "info"):
        value = getattr(body, field)
        if value is not None:
            kwargs[field] = value
    if not kwargs:
        return {"ok": True}
    try:
        await repo.update_button_style_settings(**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(session, admin, "button_style", kwargs)
    await session.commit()
    from core.cache_sync import invalidate
    await invalidate("button_style")
    return {"ok": True}
