from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory, utcnow
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository, ServiceSecuritySettings
from schemas.internal.xui import XUIClient
from services.xui.client import SanaeiXUIClient, XUIClientError, XUIRequestError
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded


logger = logging.getLogger(__name__)

DEFAULT_PLAN_DURATION_DAYS = 30
XUI_USAGE_SYNC_CONCURRENCY = 10


async def sync_xui_usage_and_status(
    session: AsyncSession,
    xui_client: SanaeiXUIClient,
    subscriptions: list[Subscription],
    security_settings: ServiceSecuritySettings | None = None,
) -> None:
    security_settings = security_settings or ServiceSecuritySettings(
        xui_limit_ip=1,
        max_distinct_ips=3,
        auto_disable_ip_abuse=True,
    )
    semaphore = asyncio.Semaphore(XUI_USAGE_SYNC_CONCURRENCY)

    async def sync_one(subscription: Subscription) -> None:
        xui_record = subscription.xui_client
        if xui_record is None:
            return

        try:
            async with semaphore:
                traffic = await xui_client.get_client_traffic(xui_record.email)
        except XUIRequestError as exc:
            error_msg = str(exc)
            # If traffic not found, the client was likely deleted from panel
            if "No traffic stats found" in error_msg or "404" in error_msg:
                logger.warning(
                    "[SYNC] Client '%s' not found on panel — marking as deleted (sub=%s)",
                    xui_record.email, subscription.id,
                )
                subscription.status = "expired"
                subscription.expired_at = utcnow()
                xui_record.is_active = False
                return
            # Other errors — skip
            logger.warning("[SYNC] Error fetching traffic for '%s': %s", xui_record.email, exc)
            return
        except XUIClientError as exc:
            logger.warning("[SYNC] Client error for '%s': %s", xui_record.email, exc)
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
            # Update X-UI panel with the real expiry time
            await _update_xui_expiry_on_activation(
                xui_client,
                subscription,
                limit_ip=security_settings.xui_limit_ip,
            )
            logger.info(
                "[SYNC] Subscription %s activated (first_use) — ends_at=%s",
                subscription.id, subscription.ends_at,
            )

        ip_abuse = await _get_ip_abuse(subscription, xui_client, security_settings)
        if subscription.status in {"pending_activation", "active"} and ip_abuse is not None:
            ip_count, ips = ip_abuse
            disabled = await _expire_subscription_in_xui(
                xui_client,
                subscription,
                now=now,
                reason=f"ip_abuse:{ip_count}",
                new_status="disabled",
                limit_ip=security_settings.xui_limit_ip,
            )
            if disabled:
                logger.warning(
                    "[SYNC] Subscription %s disabled for IP abuse (%s > %s, email=%s, ips=%s)",
                    subscription.id,
                    ip_count,
                    security_settings.max_distinct_ips,
                    xui_record.email,
                    ",".join(ips[:10]),
                )
            return

        expiry_reason = _get_expiry_reason(subscription, now)
        if subscription.status in {"pending_activation", "active"} and expiry_reason:
            expired = await _expire_subscription_in_xui(
                xui_client,
                subscription,
                now=now,
                reason=expiry_reason,
                limit_ip=security_settings.xui_limit_ip,
            )
            if not expired:
                return
            logger.info(
                "[SYNC] Subscription %s expired (%s) - UUID rotated and disabled (email=%s)",
                subscription.id, expiry_reason, xui_record.email,
            )

    await asyncio.gather(*(sync_one(subscription) for subscription in subscriptions))
    await session.flush()


async def sync_all_subscription_states() -> None:
    async with AsyncSessionFactory() as session:
        security_settings = await AppSettingsRepository(session).get_service_security_settings()
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
            try:
                async with create_xui_client_for_server(server) as xui_client:
                    await sync_xui_usage_and_status(session, xui_client, group, security_settings)
            except XUIClientError as exc:
                logger.error("[SYNC] Failed to connect to server %s: %s", server.name, exc)
                continue

        await session.commit()


async def _reset_client_uuid(
    xui_client: SanaeiXUIClient,
    subscription: Subscription,
) -> None:
    """Change client UUID in X-UI panel to immediately block access."""
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound is None:
        return

    new_uuid = str(uuid4())

    # Extract existing subId from sub_link
    existing_sub_id = ""
    current_sub_link = subscription.sub_link or (xui_record.sub_link if xui_record else "") or ""
    if current_sub_link and "/" in current_sub_link:
        existing_sub_id = current_sub_link.rsplit("/", 1)[-1]

    expiry_ms = int(subscription.ends_at.timestamp() * 1000) if subscription.ends_at is not None else 0
    updated_client = XUIClient(
        id=xui_record.xui_client_remote_id or xui_record.client_uuid,
        uuid=new_uuid,
        email=xui_record.email,
        limitIp=1,
        totalGB=subscription.volume_bytes,
        expiryTime=expiry_ms,
        enable=True,  # keep enabled so sub link works
        subId=existing_sub_id,
        comment=f"uuid_reset:{subscription.id}",
    )
    try:
        await xui_client.update_client(
            inbound_id=xui_record.inbound.xui_inbound_remote_id,
            client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            client=updated_client,
        )
        xui_record.client_uuid = new_uuid
        xui_record.is_active = False
        logger.info("[SYNC] UUID reset for client '%s' (sub=%s)", xui_record.email, subscription.id)
    except XUIClientError as exc:
        logger.warning("[SYNC] Failed to reset UUID for '%s': %s", xui_record.email, exc)


def _get_expiry_reason(subscription: Subscription, now) -> str | None:
    if subscription.volume_bytes > 0 and subscription.used_bytes >= subscription.volume_bytes:
        return "volume"
    if subscription.ends_at is not None and now >= subscription.ends_at:
        return "time"
    return None


def _extract_sub_id(subscription: Subscription, xui_record: XUIClientRecord) -> str:
    current_sub_link = subscription.sub_link or xui_record.sub_link or ""
    if current_sub_link and "/" in current_sub_link:
        return current_sub_link.rsplit("/", 1)[-1]
    return ""


async def _get_ip_abuse(
    subscription: Subscription,
    xui_client: SanaeiXUIClient,
    security_settings: ServiceSecuritySettings,
) -> tuple[int, list[str]] | None:
    if not security_settings.auto_disable_ip_abuse or security_settings.max_distinct_ips <= 0:
        return None
    xui_record = subscription.xui_client
    if xui_record is None:
        return None

    try:
        ips = await xui_client.get_client_ips(xui_record.email)
    except XUIClientError as exc:
        logger.warning("[SYNC] Failed to fetch client IPs for '%s': %s", xui_record.email, exc)
        return None

    ip_count = len(ips)
    if ip_count > security_settings.max_distinct_ips:
        return ip_count, ips
    return None


async def _expire_subscription_in_xui(
    xui_client: SanaeiXUIClient,
    subscription: Subscription,
    *,
    now,
    reason: str,
    new_status: str = "expired",
    limit_ip: int = 1,
) -> bool:
    """Mark subscription as expired but keep the client ENABLED in X-UI
    so the subscription link still works and the user can see their
    service has expired.  The X-UI panel's own traffic/time limits
    will prevent actual VPN usage.

    We do NOT rotate the UUID — that would break the sub link.
    """
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound is None:
        return False

    old_client_id = xui_record.xui_client_remote_id or xui_record.client_uuid
    # Keep the SAME UUID so the subscription link remains valid
    expired_client = XUIClient(
        id=old_client_id,
        uuid=xui_record.client_uuid,  # same UUID — sub link stays valid
        email=xui_record.email,
        limitIp=limit_ip,
        totalGB=subscription.volume_bytes,
        expiryTime=int(now.timestamp() * 1000),
        enable=True,  # keep enabled so sub link works
        subId=_extract_sub_id(subscription, xui_record),
        comment=f"expired:{reason}:{subscription.id}",
    )
    try:
        await xui_client.update_client(
            inbound_id=xui_record.inbound.xui_inbound_remote_id,
            client_id=old_client_id,
            client=expired_client,
        )
    except XUIClientError as exc:
        logger.error(
            "[SYNC] Failed to expire client '%s' for sub=%s: %s",
            xui_record.email,
            subscription.id,
            exc,
        )
        return False

    # Keep UUID unchanged, mark as inactive in our DB
    xui_record.is_active = False
    subscription.status = new_status
    subscription.expired_at = now
    return True


async def _disable_client_in_xui(
    xui_client: SanaeiXUIClient,
    subscription: Subscription,
) -> None:
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound is None:
        return

    expiry_ms = int(subscription.ends_at.timestamp() * 1000) if subscription.ends_at is not None else 0

    # Extract existing subId from sub_link
    existing_sub_id = ""
    current_sub_link = subscription.sub_link or (xui_record.sub_link if xui_record else "") or ""
    if current_sub_link and "/" in current_sub_link:
        existing_sub_id = current_sub_link.rsplit("/", 1)[-1]

    disabled_client = XUIClient(
        id=xui_record.xui_client_remote_id or xui_record.client_uuid,
        uuid=xui_record.client_uuid,
        email=xui_record.email,
        limitIp=1,
        totalGB=subscription.volume_bytes,
        expiryTime=expiry_ms,
        enable=True,  # keep enabled so sub link works
        subId=existing_sub_id,
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


async def _update_xui_expiry_on_activation(
    xui_client: SanaeiXUIClient,
    subscription: Subscription,
    *,
    limit_ip: int = 1,
) -> None:
    """Update X-UI client expiryTime when a first_use subscription is activated."""
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound is None:
        return

    expiry_ms = int(subscription.ends_at.timestamp() * 1000) if subscription.ends_at else 0
    existing_sub_id = ""
    current_sub_link = subscription.sub_link or (xui_record.sub_link if xui_record else "") or ""
    if current_sub_link and "/" in current_sub_link:
        existing_sub_id = current_sub_link.rsplit("/", 1)[-1]

    activated_client = XUIClient(
        id=xui_record.xui_client_remote_id or xui_record.client_uuid,
        uuid=xui_record.client_uuid,
        email=xui_record.email,
        limitIp=limit_ip,
        totalGB=subscription.volume_bytes,
        expiryTime=expiry_ms,
        enable=True,
        subId=existing_sub_id,
        comment=f"activated:{subscription.id}",
    )
    try:
        await xui_client.update_client(
            inbound_id=xui_record.inbound.xui_inbound_remote_id,
            client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            client=activated_client,
        )
        logger.info("[SYNC] X-UI expiry updated for '%s' — expiry_ms=%s", xui_record.email, expiry_ms)
    except XUIClientError as exc:
        logger.warning("[SYNC] Failed to update X-UI expiry for '%s': %s", xui_record.email, exc)

async def get_realtime_usage(session: AsyncSession, subscription: Subscription) -> dict | None:
    """Fetch real-time usage from X-UI panel for a single subscription."""
    xui_record = subscription.xui_client
    if xui_record is None or xui_record.inbound_id is None:
        logger.warning("[REALTIME] No xui_record or inbound_id for sub %s", subscription.id)
        return None

    # Ensure plan is loaded (needed for auto-activation duration)
    if subscription.plan is None:
        from models.plan import Plan
        plan = await session.get(Plan, subscription.plan_id)
        # Attach to subscription manually
    else:
        plan = subscription.plan

    inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(
            selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials)
        )
        .where(XUIInboundRecord.id == xui_record.inbound_id)
    )
    if inbound is None or inbound.server is None or inbound.server.credentials is None:
        logger.warning("[REALTIME] Inbound/server/credentials missing for sub %s (inbound_id=%s)", subscription.id, xui_record.inbound_id)
        return None
    xui_record.inbound = inbound

    try:
        server = ensure_inbound_server_loaded(inbound)
        security_settings = await AppSettingsRepository(session).get_service_security_settings()
        logger.info("[REALTIME] Fetching traffic for email='%s' from server '%s'", xui_record.email, server.name)
        async with create_xui_client_for_server(server) as xui_client:
            traffic = await xui_client.get_client_traffic(xui_record.email)
            logger.info("[REALTIME] Traffic result: up=%d, down=%d, used=%d", traffic.up, traffic.down, traffic.used_bytes)

            # Update local records
            now = utcnow()
            subscription.used_bytes = traffic.used_bytes
            subscription.last_usage_sync_at = now
            xui_record.usage_bytes = traffic.used_bytes

            # Auto-activate if still pending and has usage
            if subscription.status == "pending_activation" and traffic.used_bytes > 0:
                plan_duration = plan.duration_days if plan else DEFAULT_PLAN_DURATION_DAYS
                subscription.status = "active"
                subscription.activated_at = now
                subscription.starts_at = now
                subscription.ends_at = now + timedelta(days=plan_duration)
                # Update X-UI panel with the real expiry time
                try:
                    await xui_client.update_client(
                        inbound_id=inbound.xui_inbound_remote_id,
                        client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                        client=XUIClient(
                            id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                            uuid=xui_record.client_uuid,
                            email=xui_record.email,
                            limitIp=security_settings.xui_limit_ip,
                            totalGB=subscription.volume_bytes,
                            expiryTime=int(subscription.ends_at.timestamp() * 1000),
                            enable=True,
                            comment=f"activated:{subscription.id}",
                        ),
                    )
                except Exception as exc:
                    logger.warning("[REALTIME] Failed to update X-UI expiry: %s", exc)
                logger.info("[REALTIME] Auto-activated sub %s — ends_at=%s", subscription.id, subscription.ends_at)

            expiry_reason = _get_expiry_reason(subscription, now)
            ip_abuse = await _get_ip_abuse(subscription, xui_client, security_settings)
            if subscription.status in {"pending_activation", "active"} and ip_abuse is not None:
                ip_count, ips = ip_abuse
                disabled = await _expire_subscription_in_xui(
                    xui_client,
                    subscription,
                    now=now,
                    reason=f"ip_abuse:{ip_count}",
                    new_status="disabled",
                    limit_ip=security_settings.xui_limit_ip,
                )
                if disabled:
                    logger.warning(
                        "[REALTIME] Subscription %s disabled for IP abuse (%s > %s, ips=%s)",
                        subscription.id,
                        ip_count,
                        security_settings.max_distinct_ips,
                        ",".join(ips[:10]),
                    )

            if subscription.status in {"pending_activation", "active"} and expiry_reason:
                await _expire_subscription_in_xui(
                    xui_client,
                    subscription,
                    now=now,
                    reason=expiry_reason,
                    limit_ip=security_settings.xui_limit_ip,
                )

            await session.commit()

            return {
                "used_bytes": traffic.used_bytes,
                "total_bytes": subscription.volume_bytes,
                "remaining_bytes": max(subscription.volume_bytes - traffic.used_bytes, 0),
                "status": subscription.status,
            }
    except Exception as exc:
        logger.error("[REALTIME] Failed to fetch for email='%s': %s", xui_record.email, exc, exc_info=True)
        raise
