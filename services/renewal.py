from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository, RenewalSettings
from services.xui.client import SanaeiXUIClient, XUIClient
from services.xui.runtime import build_xui_client_config, ensure_inbound_server_loaded

logger = logging.getLogger(__name__)


class RenewalXUISyncError(Exception):
    """Raised when renewal was computed but X-UI panel sync failed."""


def calculate_renewal_price(
    *,
    renew_type: str,
    amount: float,
    settings: RenewalSettings,
) -> Decimal:
    if amount <= 0:
        raise ValueError("Renewal amount must be positive.")
    if renew_type == "volume":
        price = Decimal(str(amount)) * Decimal(str(settings.price_per_gb))
    elif renew_type == "time":
        price = (Decimal(str(amount)) / Decimal("10")) * Decimal(str(settings.price_per_10_days))
    else:
        raise ValueError("Invalid renewal type.")
    return price.quantize(Decimal("0.01"))


async def apply_renewal(
    *,
    session: AsyncSession,
    subscription: Subscription,
    renew_type: str,
    amount: float,
) -> None:
    """Apply renewal to subscription and sync with X-UI panel.

    Re-loads the subscription fresh from the DB to avoid stale relationship
    references (which cause 'greenlet_spawn has not been called' errors in
    async SQLAlchemy when lazy loading is implicitly triggered).
    """
    now_utc = datetime.now(timezone.utc)
    sub_id = subscription.id

    # ── Re-load subscription + xui_client fresh from DB ──────────────────
    # The subscription object passed by callers is often stale after
    # session.flush() / WalletManager.process_transaction() / begin_nested()
    # calls.  Accessing ANY relationship on a stale object triggers lazy
    # loading which is forbidden in async sessions.  Re-querying with
    # explicit column-only access guarantees safety.
    fresh = await session.scalar(
        select(Subscription).where(Subscription.id == sub_id)
    )
    if fresh is None:
        raise ValueError(f"Subscription {sub_id} not found during renewal.")

    xui = await session.scalar(
        select(XUIClientRecord).where(
            XUIClientRecord.subscription_id == sub_id
        )
    )

    # ── Apply volume/time changes ────────────────────────────────────────
    if renew_type == "volume":
        fresh.volume_bytes += int(amount * 1024**3)
        fresh.used_bytes = 0
    elif renew_type == "time":
        days_to_add = int(amount)
        if fresh.ends_at is None:
            base = fresh.activated_at or now_utc
            fresh.ends_at = base + timedelta(days=days_to_add)
        elif fresh.ends_at < now_utc:
            fresh.ends_at = now_utc + timedelta(days=days_to_add)
        else:
            fresh.ends_at += timedelta(days=days_to_add)
    else:
        raise ValueError("Invalid renewal type.")

    if fresh.status == "expired":
        fresh.status = "active"

    await session.flush()

    # ── Sync with X-UI panel ─────────────────────────────────────────────
    if xui is not None:
        try:
            await _sync_xui_limits(session, fresh, xui)
        except Exception as exc:
            # Rollback the DB changes we just flushed
            fresh.volume_bytes = subscription.volume_bytes
            fresh.used_bytes = subscription.used_bytes
            fresh.ends_at = subscription.ends_at
            fresh.status = subscription.status
            await session.flush()
            raise RenewalXUISyncError(
                f"Renewal could not be applied on X-UI panel: {exc}"
            ) from exc

    # ── Copy updated values back to the caller's object ──────────────────
    subscription.volume_bytes = fresh.volume_bytes
    subscription.used_bytes = fresh.used_bytes
    subscription.ends_at = fresh.ends_at
    subscription.status = fresh.status

    # ── Clear alert dedup keys ───────────────────────────────────────────
    try:
        from sqlalchemy import delete
        from models.app_setting import AppSetting
        await session.execute(
            delete(AppSetting).where(
                AppSetting.key.like(f"alert.sub.{sub_id}.%")
            )
        )
        await session.flush()
    except Exception as exc:
        logger.warning("Failed to clear alert keys for sub %s: %s", sub_id, exc)


async def _sync_xui_limits(
    session: AsyncSession,
    subscription: Subscription,
    xui: XUIClientRecord,
) -> None:
    xui_full = await session.scalar(
        select(XUIClientRecord)
        .options(
            selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials)
        )
        .where(XUIClientRecord.id == xui.id)
    )
    if xui_full is None or xui_full.inbound is None or xui_full.inbound.server is None:
        return

    try:
        server = ensure_inbound_server_loaded(xui_full.inbound)
        config = build_xui_client_config(server)
        now_utc = datetime.now(timezone.utc)
        # If ends_at is in the past or None, send 0 (unlimited) to X-UI
        # This prevents X-UI from immediately re-expiring the client
        if subscription.ends_at and subscription.ends_at > now_utc:
            expiry_time = int(subscription.ends_at.timestamp() * 1000)
        else:
            expiry_time = 0
        security_settings = await AppSettingsRepository(session).get_service_security_settings()
        sub_id = ""
        # Use column values only — never access relationships on subscription
        current_sub_link = subscription.sub_link or xui_full.sub_link or ""
        if "/" in current_sub_link:
            sub_id = current_sub_link.rsplit("/", 1)[-1]

        xui_client = XUIClient(
            id=xui_full.client_uuid,
            uuid=xui_full.client_uuid,
            email=xui_full.email,
            enable=True,
            limitIp=security_settings.xui_limit_ip,
            totalGB=subscription.volume_bytes,
            expiryTime=expiry_time,
            subId=sub_id,
            comment=xui_full.username or "",
        )
        xui_full.is_active = True
        async with SanaeiXUIClient(config) as client:
            await client.update_client(
                inbound_id=xui_full.inbound.xui_inbound_remote_id,
                client_id=xui_full.client_uuid,
                client=xui_client,
            )
    except Exception as exc:
        logger.error("Failed to sync X-UI limits after renewal: %s", exc, exc_info=True)
        raise RenewalXUISyncError(
            f"Renewal could not be applied on X-UI panel: {exc}"
        ) from exc
