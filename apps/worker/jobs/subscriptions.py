from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory, utcnow
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from schemas.internal.xui import XUIClient
from services.xui.client import SanaeiXUIClient, XUIClientError
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded


DEFAULT_PLAN_DURATION_DAYS = 30
XUI_USAGE_SYNC_CONCURRENCY = 10


async def sync_xui_usage_and_status(
    session: AsyncSession,
    xui_client: SanaeiXUIClient,
    subscriptions: list[Subscription],
) -> None:
    semaphore = asyncio.Semaphore(XUI_USAGE_SYNC_CONCURRENCY)

    async def sync_one(subscription: Subscription) -> None:
        xui_record = subscription.xui_client
        if xui_record is None:
            return

        try:
            async with semaphore:
                traffic = await xui_client.get_client_traffic(xui_record.email)
        except XUIClientError:
            return

        now = utcnow()
        plan_duration_days = subscription.plan.duration_days if subscription.plan is not None else DEFAULT_PLAN_DURATION_DAYS
        subscription.used_bytes = traffic.used_bytes
        subscription.last_usage_sync_at = now
        xui_record.usage_bytes = traffic.used_bytes

        if subscription.status == "pending_activation" and traffic.used_bytes > 0:
            subscription.status = "active"
            subscription.activated_at = now
            subscription.starts_at = now
            subscription.ends_at = now + timedelta(days=plan_duration_days)

        should_expire_for_volume = subscription.used_bytes >= subscription.volume_bytes > 0
        should_expire_for_time = subscription.ends_at is not None and now > subscription.ends_at
        if subscription.status in {"pending_activation", "active"} and (
            should_expire_for_volume or should_expire_for_time
        ):
            await _disable_client_in_xui(xui_client, subscription)
            subscription.status = "expired"
            subscription.expired_at = now
            xui_record.is_active = False

    await asyncio.gather(*(sync_one(subscription) for subscription in subscriptions))
    await session.flush()


async def sync_all_subscription_states() -> None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.plan),
                selectinload(Subscription.xui_client)
                .selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials),
            )
            .where(Subscription.status.in_(["pending_activation", "active"]))
        )
        subscriptions = list(result.scalars().all())

        grouped_by_server: dict[str, list[Subscription]] = {}
        for subscription in subscriptions:
            xui_record = subscription.xui_client
            if xui_record is None or xui_record.inbound is None:
                continue
            server = xui_record.inbound.server
            if server is None or server.credentials is None or not server.is_active:
                continue
            grouped_by_server.setdefault(str(server.id), []).append(subscription)

        for group in grouped_by_server.values():
            sample_subscription = group[0]
            sample_inbound = sample_subscription.xui_client.inbound if sample_subscription.xui_client is not None else None
            if sample_inbound is None:
                continue
            server = ensure_inbound_server_loaded(sample_inbound)
            async with create_xui_client_for_server(server) as xui_client:
                await sync_xui_usage_and_status(session, xui_client, group)
        await session.commit()


async def _disable_client_in_xui(
    xui_client: SanaeiXUIClient,
    subscription: Subscription,
) -> None:
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound is None:
        return

    expiry_ms = int(subscription.ends_at.timestamp() * 1000) if subscription.ends_at is not None else 0
    disabled_client = XUIClient(
        id=xui_record.xui_client_remote_id or xui_record.client_uuid,
        uuid=xui_record.client_uuid,
        email=xui_record.email,
        limitIp=1,
        totalGB=subscription.volume_bytes,
        expiryTime=expiry_ms,
        enable=False,
        comment=f"expired:{subscription.id}",
    )
    try:
        await xui_client.update_client(
            inbound_id=xui_record.inbound.xui_inbound_remote_id,
            client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            client=disabled_client,
        )
    except XUIClientError:
        return
