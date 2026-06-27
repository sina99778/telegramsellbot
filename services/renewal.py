from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.plan import Plan
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository, RenewalSettings, ServiceSecuritySettings
from services.panels.marzban import marzban_client_for_server, record_is_marzban_family
from schemas.internal.pasarguard import PGUserModify
from services.xui.client import SanaeiXUIClient, XUIClient
from services.xui.runtime import build_xui_client_config, ensure_inbound_server_loaded

logger = logging.getLogger(__name__)


class RenewalXUISyncError(Exception):
    """Raised when renewal was computed but X-UI panel sync failed."""


class RenewalNotAllowedError(Exception):
    """Raised when a renewal is not permitted (e.g. extending TIME on a config
    that hasn't been activated yet). Carries a user-facing Persian message."""


# A TIME renewal applied BEFORE first connect is silently discarded: on first
# use the activation recomputes ends_at from scratch on BOTH panels (X-UI via
# _update_xui_expiry_on_activation, PasarGuard via the on_hold timer + usage
# sync), so the pre-activation days never survive. Block it instead of taking
# money for nothing — volume can still be added while pending.
PENDING_TIME_RENEWAL_MSG = (
    "⏳ این سرویس هنوز فعال نشده (زمان از اولین اتصال شروع می‌شود). "
    "تمدیدِ زمان بعد از فعال‌سازی ممکن است؛ فعلاً فقط می‌توانید حجم اضافه کنید."
)


def time_renewal_blocked(subscription, renew_type: str) -> bool:
    """True for a TIME renewal of a not-yet-activated (pending_activation) config."""
    return renew_type == "time" and getattr(subscription, "status", None) == "pending_activation"


# A VOLUME renewal cannot resurrect a TIME-expired config: apply_renewal would
# flip status to active while ends_at stays in the past — X-UI then gets
# expiryTime=0 (briefly UNLIMITED until the next sync re-expires it) and the
# Marzban payload drops expire=None entirely so the panel user stays expired —
# either way the user paid for volume on a dead config. Block it and tell the
# user to renew time first (or together); volume-expired configs with remaining
# time renew normally.
TIME_EXPIRED_VOLUME_RENEWAL_MSG = (
    "⏳ مدت‌زمان این سرویس به پایان رسیده است. "
    "ابتدا زمان سرویس را تمدید کنید؛ بعد از آن می‌توانید حجم اضافه کنید."
)


def volume_renewal_blocked(subscription, renew_type: str) -> bool:
    """True for a VOLUME renewal of a config whose TIME has already run out."""
    if renew_type != "volume":
        return False
    ends_at = getattr(subscription, "ends_at", None)
    return ends_at is not None and ends_at <= datetime.now(timezone.utc)


async def average_active_plan_renewal_rates(
    session: AsyncSession, settings: RenewalSettings
) -> tuple[float, float]:
    """Return (per_gb, per_day) = the AVERAGE of the currently-active plans'
    effective renewal rates.

    Used to price configs that have NO plan (migrated / imported from the legacy
    bot): per the operator's choice they renew at the average of the active
    catalogue's rates rather than the bare global rate. Each plan contributes
    its own per-gb/per-day override, or the global rate when it hasn't set one.
    Falls back to the global rate when there are no active plans.
    """
    plans = list(
        (await session.execute(select(Plan).where(Plan.is_active.is_(True)))).scalars().all()
    )
    if not plans:
        return (float(settings.price_per_gb), float(settings.price_per_10_days) / 10.0)
    gb_vals = [p.effective_renewal_price_per_gb(settings.price_per_gb) for p in plans]
    day_vals = [p.effective_renewal_price_per_day(settings.price_per_10_days) for p in plans]
    return (sum(gb_vals) / len(gb_vals), sum(day_vals) / len(day_vals))


def calculate_renewal_price(
    *,
    renew_type: str,
    amount: float,
    settings: RenewalSettings,
    plan: Plan | None = None,
    default_per_gb: float | None = None,
    default_per_day: float | None = None,
) -> Decimal:
    """Compute renewal price.

    If `plan` is provided AND has the matching per-plan override set, we prefer
    it. Otherwise, for plan-less configs, `default_per_gb` / `default_per_day`
    (e.g. the average of active plans for migrated configs) are used when given;
    falling back to the global `settings` defaults (per-10-days / 10).
    """
    if amount <= 0:
        raise ValueError("Renewal amount must be positive.")
    if renew_type == "volume":
        if plan is not None:
            per_gb = plan.effective_renewal_price_per_gb(settings.price_per_gb)
        elif default_per_gb is not None:
            per_gb = default_per_gb
        else:
            per_gb = settings.price_per_gb
        price = Decimal(str(amount)) * Decimal(str(per_gb))
    elif renew_type == "time":
        if plan is not None:
            per_day = plan.effective_renewal_price_per_day(settings.price_per_10_days)
        elif default_per_day is not None:
            per_day = default_per_day
        else:
            per_day = float(settings.price_per_10_days) / 10.0
        price = Decimal(str(amount)) * Decimal(str(per_day))
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

    Uses begin_nested() (savepoint) so that if X-UI sync fails, ALL DB
    changes are automatically rolled back by PostgreSQL.

    CRITICAL: Inside the savepoint, we ONLY access column attributes on
    the subscription object (volume_bytes, used_bytes, ends_at, status,
    sub_link, activated_at, id).  We NEVER access relationship attributes
    (user, order, plan, xui_client) because that triggers SQLAlchemy
    lazy loading which is forbidden in async sessions and causes the
    'greenlet_spawn has not been called' error.

    The xui_client is loaded via an explicit query BEFORE the savepoint.
    """
    # Safety net: never extend TIME on a not-yet-activated config (the days are
    # discarded on first connect). Callers should block this earlier with a nice
    # message; this guard protects any path that doesn't.
    if time_renewal_blocked(subscription, renew_type):
        raise RenewalNotAllowedError(PENDING_TIME_RENEWAL_MSG)
    # Safety net #2: never add VOLUME to a time-expired config (it cannot come
    # back to life from volume alone — see TIME_EXPIRED_VOLUME_RENEWAL_MSG).
    if volume_renewal_blocked(subscription, renew_type):
        raise RenewalNotAllowedError(TIME_EXPIRED_VOLUME_RENEWAL_MSG)

    now_utc = datetime.now(timezone.utc)
    sub_id = subscription.id

    # Fetch settings before any modifications to avoid auto-flush lazy load errors
    security_settings = await AppSettingsRepository(session).get_service_security_settings()

    # Load the Plan up-front so we can resolve per-plan ip_limit without
    # touching subscription.plan inside the savepoint (lazy-load forbidden).
    plan: Plan | None = None
    if subscription.plan_id is not None:
        plan = await session.scalar(select(Plan).where(Plan.id == subscription.plan_id))

    # ── Load xui_client via explicit query ───────────────────────────────
    # NEVER use subscription.xui_client — that triggers lazy loading!
    xui = await session.scalar(
        select(XUIClientRecord).where(
            XUIClientRecord.subscription_id == sub_id
        )
    )

    # ── Savepoint: if X-UI sync fails, all DB changes auto-rollback ─────
    async with session.begin_nested():
        with session.no_autoflush:
            # Only modify COLUMN attributes — never touch relationships!
            if renew_type == "volume":
                subscription.volume_bytes += int(amount * 1024**3)
                # Deliberately NO lifetime accumulation / used_bytes reset here.
                # The panel traffic counter is CUMULATIVE and is never reset on
                # renewal, and the usage-sync job writes it back absolutely
                # (subscription.used_bytes = panel counter) within a minute. So
                # used_bytes is cumulative per panel client, volume_bytes is
                # cumulative per subscription, and Total = lifetime + used stays
                # continuously correct. Rolling used into lifetime here (the old
                # behavior) double-counted the entire cumulative usage on every
                # volume renewal once the sync restored used_bytes.
                # lifetime_used_bytes accumulates ONLY where a new panel client
                # (counter genuinely back to 0) replaces the old one — the
                # migration paths in services/provisioning/manager.py.
            elif renew_type == "time":
                days_to_add = int(amount)
                if subscription.ends_at is None:
                    base = subscription.activated_at or now_utc
                    subscription.ends_at = base + timedelta(days=days_to_add)
                elif subscription.ends_at < now_utc:
                    subscription.ends_at = now_utc + timedelta(days=days_to_add)
                else:
                    subscription.ends_at += timedelta(days=days_to_add)
            elif renew_type == "plan":
                if plan is not None:
                    subscription.volume_bytes += plan.volume_bytes
                    days_to_add = plan.duration_days
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

            # Sync with X-UI panel — if this fails, the SAVEPOINT rolls back
            # all the DB changes above automatically.
            if xui is not None:
                await _sync_xui_limits(session, subscription, xui, security_settings, plan=plan)

            await session.flush()

    # ── Clear alert dedup keys (with a savepoint) ────────────────────────
    try:
        async with session.begin_nested():
            from sqlalchemy import delete
            from models.app_setting import AppSetting
            await session.execute(
                delete(AppSetting).where(
                    AppSetting.key.like(f"alert.sub.{sub_id}.%")
                )
            )
    except Exception as exc:
        logger.warning("Failed to clear alert keys for sub %s: %s", sub_id, exc)


async def _sync_xui_limits(
    session: AsyncSession,
    subscription: Subscription,
    xui: XUIClientRecord,
    security_settings: ServiceSecuritySettings,
    *,
    plan: Plan | None = None,
) -> None:
    """Sync subscription limits to X-UI panel.

    Loads xui_full with full eager loading (inbound → server → credentials).
    Only reads COLUMN attributes from subscription (ends_at, volume_bytes,
    sub_link).  Never accesses any relationship on subscription.
    """
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

    # Marzban-family panels (PasarGuard / Rebecca) renew via the user-centric API.
    if record_is_marzban_family(xui_full):
        await _sync_pasarguard_limits(subscription, xui_full)
        return

    try:
        server = ensure_inbound_server_loaded(xui_full.inbound)
        config = build_xui_client_config(server)
        now_utc = datetime.now(timezone.utc)

        # Column access only — safe in async
        if subscription.ends_at and subscription.ends_at > now_utc:
            expiry_time = int(subscription.ends_at.timestamp() * 1000)
        else:
            expiry_time = 0

        # Extract sub_id from sub_link (column attributes only)
        sub_id_str = ""
        current_sub_link = subscription.sub_link or xui_full.sub_link or ""
        if "/" in current_sub_link:
            sub_id_str = current_sub_link.rsplit("/", 1)[-1]

        # Resolve the IP cap: per-plan override (if set on Plan) wins.
        ip_limit = plan.effective_ip_limit(security_settings.xui_limit_ip) if plan else security_settings.xui_limit_ip

        # The panel addresses a client by its remote id (URL path + body "id").
        # For imported rows the local client_uuid can be empty, so prefer the
        # remote id captured at provisioning/import time and fall back to the
        # uuid. Without this the panel rejects the call with "Something went
        # wrong (empty client ID)". The URL client_id and the body id MUST be
        # the same value (this mirrors the proven auto-activation path).
        client_id = xui_full.xui_client_remote_id or xui_full.client_uuid
        if not client_id:
            logger.warning(
                "Skipping X-UI limit sync for sub=%s xui=%s: no client id on record",
                subscription.id, xui_full.id,
            )
            return

        xui_client = XUIClient(
            id=client_id,
            uuid=xui_full.client_uuid,
            email=xui_full.email,
            enable=True,
            limitIp=ip_limit,
            totalGB=subscription.volume_bytes,
            expiryTime=expiry_time,
            subId=sub_id_str,
            comment=xui_full.username or "",
        )
        xui_full.is_active = True
        async with SanaeiXUIClient(config) as client:
            await client.update_client(
                inbound_id=xui_full.inbound.xui_inbound_remote_id,
                client_id=client_id,
                client=xui_client,
            )
    except Exception as exc:
        logger.error("Failed to sync X-UI limits after renewal: %s", exc, exc_info=True)
        raise RenewalXUISyncError(
            f"Renewal could not be applied on X-UI panel: {exc}"
        ) from exc


async def _sync_pasarguard_limits(
    subscription: Subscription,
    xui_full: XUIClientRecord,
) -> None:
    """Push renewed limits to PasarGuard (PUT data_limit / expire / status).

    apply_renewal already did the panel-agnostic column math (volume/time +
    lifetime accumulation), so we just mirror the resulting state to the panel.
    Notes:
      * PG `expire` is UNIX SECONDS (X-UI used ms).
      * We only force status="active" + expire when the sub is already active
        (e.g. an expired sub that apply_renewal just reactivated). A still
        on_hold (pending_activation) sub gets ONLY its data_limit bumped, so its
        first-connect timer stays intact.
    """
    server = xui_full.inbound.server
    username = xui_full.panel_username or xui_full.username
    now_utc = datetime.now(timezone.utc)

    data_limit = int(subscription.volume_bytes) or None  # 0 => unlimited

    if subscription.status == "active":
        if subscription.ends_at and subscription.ends_at > now_utc:
            expire = int(subscription.ends_at.timestamp())  # seconds
        else:
            expire = None  # unlimited duration
        payload = PGUserModify(status="active", expire=expire, data_limit=data_limit)
    else:
        # Still on_hold — only adjust the quota; keep the first-use timer.
        payload = PGUserModify(data_limit=data_limit)

    try:
        async with marzban_client_for_server(server) as client:
            await client.modify_user(username, payload)
        xui_full.is_active = True
    except Exception as exc:
        logger.error("Failed to sync Marzban-family limits after renewal: %s", exc, exc_info=True)
        raise RenewalXUISyncError(
            f"Renewal could not be applied on the panel: {exc}"
        ) from exc
