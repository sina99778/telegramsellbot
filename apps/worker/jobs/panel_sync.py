"""Replay committed desired panel state, independently of provider callbacks."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory, utcnow
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from services.provisioning.manager import ProvisioningManager
from services.renewal import _sync_xui_limits

logger = logging.getLogger(__name__)


async def deliver_panel_state(session: AsyncSession, subscription: Subscription) -> bool:
    """Caller holds the subscription row lock until commit."""
    attempted = subscription.panel_sync_attempted_at
    attempts = int(subscription.panel_sync_attempts or 0)
    if (
        attempted
        and attempted + timedelta(seconds=min(30 * 2 ** min(attempts, 7), 3600)) > utcnow()
    ):
        return False
    subscription.panel_sync_attempted_at = utcnow()
    subscription.panel_sync_attempts = attempts + 1
    record = subscription.xui_client
    try:
        if record is None:
            raise ValueError("PanelMappingMissing")
        async with asyncio.timeout(60):
            if subscription.status in {"active", "pending_activation"}:
                security = await AppSettingsRepository(session).get_service_security_settings()
                await _sync_xui_limits(
                    session,
                    subscription,
                    record,
                    security,
                    plan=subscription.plan,
                )
                record.is_active = True
            else:
                await ProvisioningManager(session)._disable_xui_client(
                    xui_record=record,
                    volume_bytes=subscription.volume_bytes,
                    ends_at=subscription.ends_at,
                    sub_link=subscription.sub_link,
                )
                record.is_active = False
    except Exception as exc:
        # Keep the desired state and its pending marker. Only a redacted class
        # is persisted; HTTP error strings may contain bearer subscription URLs.
        subscription.panel_sync_error = type(exc).__name__[:128]
        logger.warning("Panel sync deferred for %s (%s)", subscription.id, type(exc).__name__)
        return False
    subscription.panel_sync_pending = False
    subscription.panel_sync_error = None
    return True


async def sync_pending_panel_states() -> None:
    async with AsyncSessionFactory() as session:
        ids = list(
            (
                await session.scalars(
                    select(Subscription.id)
                    .where(Subscription.panel_sync_pending.is_(True))
                    .order_by(
                        Subscription.panel_sync_attempted_at.asc().nullsfirst(), Subscription.id
                    )
                    .limit(50)
                )
            ).all()
        )
    semaphore = asyncio.Semaphore(4)

    async def deliver_one(subscription_id: UUID) -> None:
        async with semaphore:
            async with AsyncSessionFactory() as session:
                try:
                    subscription = await session.scalar(
                        select(Subscription)
                        .where(
                            Subscription.id == subscription_id,
                            Subscription.panel_sync_pending.is_(True),
                        )
                        .options(
                            selectinload(Subscription.plan),
                            selectinload(Subscription.xui_client)
                            .selectinload(XUIClientRecord.inbound)
                            .selectinload(XUIInboundRecord.server)
                            .selectinload(XUIServerRecord.credentials),
                        )
                        .with_for_update(skip_locked=True, of=Subscription)
                    )
                    if subscription is None:
                        return
                    delivered = await deliver_panel_state(session, subscription)
                    await session.commit()
                    if delivered:
                        logger.info(
                            "Panel sync delivered for subscription %s after %d attempts",
                            subscription_id, subscription.panel_sync_attempts,
                        )
                except Exception:
                    await session.rollback()
                    logger.exception("Panel outbox transaction failed for %s", subscription_id)

    try:
        async with asyncio.timeout(120):
            await asyncio.gather(*(deliver_one(subscription_id) for subscription_id in ids))
    except TimeoutError:
        # Cancellation rolls back unfinished rows; their committed outbox
        # markers survive and the absolute writes are safe to replay.
        logger.warning("Panel outbox batch deadline reached; pending rows will retry")
