from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory, utcnow
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository, ServiceSecuritySettings
from schemas.internal.xui import XUIClient
from services.panels.marzban import (
    is_marzban_family,
    marzban_client_for_server,
    record_is_marzban_family,
)
from services.pasarguard.client import PasarGuardError
from services.xui.client import SanaeiXUIClient, XUIClientError, XUIRequestError
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded


logger = logging.getLogger(__name__)

DEFAULT_PLAN_DURATION_DAYS = 30
XUI_USAGE_SYNC_CONCURRENCY = 10
# How many CONSECUTIVE "client not found on panel" sync cycles before we trust
# it and mark the sub expired. A single transient panel error (or a flaky
# "no traffic stats found") must never expire a config that still has valid
# time + volume. Sync runs ~every minute, so 5 ≈ 5 minutes of sustained gone.
USAGE_GONE_STRIKES = 5


async def sync_xui_usage_and_status(
    session: AsyncSession,
    xui_client: SanaeiXUIClient,
    subscriptions: list[Subscription],
    security_settings: ServiceSecuritySettings | None = None,
) -> bool:
    """Sync usage and status for subscriptions on one server.

    Returns True if any subscription expired (caller should restart Xray).
    """
    security_settings = security_settings or ServiceSecuritySettings(
        xui_limit_ip=1,
        max_distinct_ips=3,
        auto_disable_ip_abuse=True,
    )
    semaphore = asyncio.Semaphore(XUI_USAGE_SYNC_CONCURRENCY)
    any_expired = False

    async def sync_one(subscription: Subscription) -> None:
        nonlocal any_expired
        xui_record = subscription.xui_client
        if xui_record is None:
            return

        try:
            async with semaphore:
                traffic = await xui_client.get_client_traffic(xui_record.email)
        except XUIRequestError as exc:
            error_msg = str(exc)
            low = error_msg.lower()
            # Does the panel ACTIVELY say this client is gone? ("No traffic stats
            # found", a 404, or "Inbound Not Found For Email"). A transient
            # network/5xx error is NOT this and must never expire a config.
            client_gone = (
                "no traffic stats found" in low
                or "404" in error_msg
                or "not found" in low  # covers "Inbound Not Found For Email"
            )
            if client_gone:
                # Count consecutive strikes. Only after several in a row do we
                # trust it and expire — a single flaky response (the panel
                # sometimes returns "no traffic stats found" for a live client)
                # used to instantly expire a perfectly valid config.
                strikes = (subscription.usage_sync_failures or 0) + 1
                subscription.usage_sync_failures = strikes
                if strikes >= USAGE_GONE_STRIKES:
                    logger.warning(
                        "[SYNC] Client '%s' reported gone %d cycles in a row — marking expired (sub=%s)",
                        xui_record.email, strikes, subscription.id,
                    )
                    subscription.status = "expired"
                    subscription.expired_at = utcnow()
                    xui_record.is_active = False
                    any_expired = True
                else:
                    logger.info(
                        "[SYNC] Client '%s' not found (strike %d/%d) — leaving sub untouched (sub=%s)",
                        xui_record.email, strikes, USAGE_GONE_STRIKES, subscription.id,
                    )
                return
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
        subscription.usage_sync_failures = 0  # a successful read clears the strikes

        # Recover a config that was previously (falsely) marked expired but is
        # clearly ALIVE on the panel and still has valid time + volume. This
        # auto-undoes the old "transient panel error → expired" damage that left
        # working configs showing «منقضی» while still active on the panel.
        if subscription.status == "expired":
            time_ok = subscription.ends_at is None or subscription.ends_at > now
            vol_ok = subscription.volume_bytes <= 0 or traffic.used_bytes < subscription.volume_bytes
            if time_ok and vol_ok:
                subscription.status = "active"
                subscription.expired_at = None
                xui_record.is_active = True
                logger.info(
                    "[SYNC] Reactivated falsely-expired sub %s (alive on panel, time+volume OK)",
                    subscription.id,
                )
            else:
                # Genuinely expired (time/volume) — leave as-is, don't reprocess.
                return

        if subscription.status == "pending_activation" and traffic.used_bytes > 0:
            subscription.status = "active"
            subscription.activated_at = now
            subscription.starts_at = now
            subscription.ends_at = now + timedelta(days=plan_duration_days)
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
                any_expired = True
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
            any_expired = True
            logger.info(
                "[SYNC] Subscription %s expired (%s) - kept enabled for sub link (email=%s)",
                subscription.id, expiry_reason, xui_record.email,
            )

    # return_exceptions=True so one row raising an UNEXPECTED (non-XUI) error —
    # e.g. malformed JSON / a pydantic ValidationError from the panel response —
    # cannot abort the whole batch and skip the flush below (which would discard
    # every other row's usage/status updates and bypass the cycle's commit).
    results = await asyncio.gather(
        *(sync_one(subscription) for subscription in subscriptions),
        return_exceptions=True,
    )
    for subscription, res in zip(subscriptions, results):
        if isinstance(res, Exception):
            logger.warning("[SYNC] unexpected error syncing sub %s: %s", subscription.id, res)
    await session.flush()
    return any_expired


async def sync_pasarguard_usage_and_status(
    session: AsyncSession,
    server: XUIServerRecord,
    subscriptions: list[Subscription],
) -> None:
    """Sync usage + status for PasarGuard configs on one server.

    Unlike X-UI (where WE set the expiry on first traffic), PasarGuard manages
    on_hold→active itself: the timer starts on first connect and the panel
    computes `expire`. So we READ the panel's status/expire and mirror it into
    our Subscription. No Xray restart, no IP-abuse probe (PasarGuard doesn't
    expose per-user IPs in our surface).
    """
    now = utcnow()
    async with marzban_client_for_server(server) as client:
        for subscription in subscriptions:
            # Per-row isolation: a single bad row (e.g. the panel returns a 200
            # whose JSON fails PGUserResponse validation, which is NOT a
            # PasarGuardError) must NOT abort syncing the rest of the batch.
            try:
                xui_record = subscription.xui_client
                if xui_record is None:
                    continue
                username = xui_record.panel_username or xui_record.username
                plan_duration_days = (
                    subscription.plan.duration_days
                    if subscription.plan is not None
                    else DEFAULT_PLAN_DURATION_DAYS
                )

                pg_user = await client.get_user(username)

                if pg_user is None:
                    # 404 — config gone on the panel. Strike before trusting it.
                    strikes = (subscription.usage_sync_failures or 0) + 1
                    subscription.usage_sync_failures = strikes
                    if strikes >= USAGE_GONE_STRIKES:
                        subscription.status = "expired"
                        subscription.expired_at = now
                        xui_record.is_active = False
                        logger.warning(
                            "[SYNC][PG] user '%s' gone %d cycles — expired (sub=%s)",
                            username, strikes, subscription.id,
                        )
                    continue

                # Usage (panel is the source of truth).
                used = int(pg_user.used_traffic or 0)
                subscription.used_bytes = used
                xui_record.usage_bytes = used
                subscription.last_usage_sync_at = now
                subscription.usage_sync_failures = 0

                status = (pg_user.status or "").lower()
                pg_expire = pg_user.expire_ts
                pg_ends_at = (
                    datetime.fromtimestamp(pg_expire, tz=timezone.utc) if pg_expire else None
                )

                # First-connect activation: PasarGuard flips on_hold→active and sets
                # expire itself — we just record it (no panel write).
                if subscription.status == "pending_activation" and status == "active":
                    subscription.status = "active"
                    subscription.activated_at = now
                    subscription.starts_at = now
                    subscription.ends_at = pg_ends_at or (now + timedelta(days=plan_duration_days))
                    xui_record.is_active = True
                    logger.info(
                        "[SYNC][PG] sub %s activated (first connect) ends_at=%s",
                        subscription.id, subscription.ends_at,
                    )
                    continue

                # Panel says finished → expire on our side too.
                if status in {"expired", "limited"}:
                    if subscription.status != "expired":
                        subscription.status = "expired"
                        subscription.expired_at = now
                        xui_record.is_active = False
                        logger.info("[SYNC][PG] sub %s expired (panel status=%s)", subscription.id, status)
                    continue

                # Recover a falsely-expired sub that the panel reports alive.
                if subscription.status == "expired" and status == "active":
                    subscription.status = "active"
                    subscription.expired_at = None
                    xui_record.is_active = True
                    if pg_ends_at is not None:
                        subscription.ends_at = pg_ends_at
                    logger.info("[SYNC][PG] reactivated sub %s (alive on panel)", subscription.id)
                    continue

                # Active & alive — keep ends_at aligned with the panel's authority.
                if subscription.status == "active" and status == "active":
                    if pg_ends_at is not None:
                        subscription.ends_at = pg_ends_at
                    xui_record.is_active = True
            except Exception as exc:  # noqa: BLE001 — isolate one bad row
                logger.warning("[SYNC][PG] error syncing sub %s: %s", subscription.id, exc)
                continue

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
            .where(
                or_(
                    Subscription.status.in_(["pending_activation", "active"]),
                    # Re-check configs that are marked expired but STILL have a
                    # future end date AND un-exhausted volume — the signature of
                    # a falsely-expired config. If the panel confirms it's alive,
                    # sync_one reactivates it. They drop out of this filter once
                    # reactivated, so there's no permanent extra load.
                    and_(
                        Subscription.status == "expired",
                        Subscription.ends_at.isnot(None),
                        Subscription.ends_at > utcnow(),
                        or_(
                            Subscription.volume_bytes <= 0,
                            Subscription.used_bytes < Subscription.volume_bytes,
                        ),
                    ),
                )
            )
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

            # PasarGuard servers sync via the user-centric API (no Xray restart).
            if is_marzban_family(server):
                try:
                    await sync_pasarguard_usage_and_status(session, server, group)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[SYNC] PasarGuard sync failed for server %s: %s", server.name, exc)
                continue

            try:
                async with create_xui_client_for_server(server) as xui_client:
                    any_expired = await sync_xui_usage_and_status(session, xui_client, group, security_settings)
                    # Restart Xray core after expiry to enforce limits immediately
                    if any_expired:
                        try:
                            await xui_client.restart_xray_core()
                            logger.info("[SYNC] Xray core restarted on server '%s' after config expiry", server.name)
                        except Exception as restart_exc:
                            logger.warning("[SYNC] Failed to restart Xray on server '%s': %s", server.name, restart_exc)
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

    # PasarGuard configs read realtime usage/status from the user-centric API.
    if record_is_marzban_family(xui_record):
        return await _pasarguard_realtime_usage(session, subscription, xui_record, inbound)

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
            subscription.usage_sync_failures = 0

            # Recover a falsely-expired config immediately when the user opens
            # it: alive on the panel + valid time + volume → reactivate. Mirrors
            # the same recovery in the background sync job.
            if subscription.status == "expired":
                _time_ok = subscription.ends_at is None or subscription.ends_at > now
                _vol_ok = subscription.volume_bytes <= 0 or traffic.used_bytes < subscription.volume_bytes
                if _time_ok and _vol_ok:
                    subscription.status = "active"
                    subscription.expired_at = None
                    xui_record.is_active = True
                    logger.info("[REALTIME] Reactivated falsely-expired sub %s on view", subscription.id)

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

            # NOTE: this function is only ever called from a bot request
            # handler (my_configs view/refresh), where DatabaseSessionMiddleware
            # commits at the end of the update. We only flush here so the usage
            # UPDATEs are sent to the DB inside the handler's transaction —
            # committing here would prematurely close the handler's transaction
            # boundary (partial persistence on a later error). Do NOT switch
            # this back to commit().
            await session.flush()

            return {
                "used_bytes": traffic.used_bytes,
                "total_bytes": subscription.volume_bytes,
                "remaining_bytes": max(subscription.volume_bytes - traffic.used_bytes, 0),
                "status": subscription.status,
            }
    except Exception as exc:
        logger.error("[REALTIME] Failed to fetch for email='%s': %s", xui_record.email, exc, exc_info=True)
        raise


async def _pasarguard_realtime_usage(
    session: AsyncSession,
    subscription: Subscription,
    xui_record: XUIClientRecord,
    inbound: XUIInboundRecord,
) -> dict | None:
    """Single-config realtime read for PasarGuard (the my_configs 'refresh'
    button). Mirrors the batch sync's transitions for one user."""
    server = ensure_inbound_server_loaded(inbound)
    username = xui_record.panel_username or xui_record.username
    plan_duration = (
        subscription.plan.duration_days if subscription.plan is not None else DEFAULT_PLAN_DURATION_DAYS
    )
    now = utcnow()

    async with marzban_client_for_server(server) as client:
        pg_user = await client.get_user(username)

    if pg_user is None:
        # Don't mutate on a single missing read — just report current state.
        return {
            "used_bytes": subscription.used_bytes or 0,
            "total_bytes": subscription.volume_bytes,
            "remaining_bytes": max(subscription.volume_bytes - (subscription.used_bytes or 0), 0),
            "status": subscription.status,
        }

    used = int(pg_user.used_traffic or 0)
    subscription.used_bytes = used
    xui_record.usage_bytes = used
    subscription.last_usage_sync_at = now
    subscription.usage_sync_failures = 0

    status = (pg_user.status or "").lower()
    pg_expire = pg_user.expire_ts
    pg_ends_at = datetime.fromtimestamp(pg_expire, tz=timezone.utc) if pg_expire else None

    if subscription.status == "pending_activation" and status == "active":
        subscription.status = "active"
        subscription.activated_at = now
        subscription.starts_at = now
        subscription.ends_at = pg_ends_at or (now + timedelta(days=plan_duration))
        xui_record.is_active = True
    elif status in {"expired", "limited"} and subscription.status != "expired":
        subscription.status = "expired"
        subscription.expired_at = now
        xui_record.is_active = False
    elif subscription.status == "expired" and status == "active":
        subscription.status = "active"
        subscription.expired_at = None
        xui_record.is_active = True
        if pg_ends_at is not None:
            subscription.ends_at = pg_ends_at
    elif subscription.status == "active" and status == "active" and pg_ends_at is not None:
        subscription.ends_at = pg_ends_at

    await session.flush()
    return {
        "used_bytes": used,
        "total_bytes": subscription.volume_bytes,
        "remaining_bytes": max(subscription.volume_bytes - used, 0),
        "status": subscription.status,
    }
