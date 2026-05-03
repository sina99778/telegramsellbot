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
    now_utc = datetime.now(timezone.utc)

    if renew_type == "volume":
        subscription.volume_bytes += int(amount * 1024**3)
    elif renew_type == "time":
        days_to_add = int(amount)
        if subscription.ends_at is None:
            base = subscription.activated_at or now_utc
            subscription.ends_at = base + timedelta(days=days_to_add)
        elif subscription.ends_at < now_utc:
            subscription.ends_at = now_utc + timedelta(days=days_to_add)
        else:
            subscription.ends_at += timedelta(days=days_to_add)
    else:
        raise ValueError("Invalid renewal type.")

    if subscription.status == "expired":
        subscription.status = "active"

    xui = subscription.xui_client
    if xui is not None:
        await _sync_xui_limits(session, subscription, xui)

    await session.flush()


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
        expiry_time = int(subscription.ends_at.timestamp() * 1000) if subscription.ends_at else 0
        security_settings = await AppSettingsRepository(session).get_service_security_settings()
        sub_id = ""
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
