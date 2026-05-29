from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.order import Order
from models.plan import Plan
from models.ready_config import ReadyConfigItem, ReadyConfigPool
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from schemas.internal.xui import XUIClient
from services.plan_inventory import PlanStockError, release_plan_sale, reserve_plan_sale
from services.wallet.manager import WalletManager
from services.xui.client import SanaeiXUIClient
from services.xui.runtime import build_sub_link, build_vless_uri, create_xui_client_for_server, ensure_inbound_server_loaded


logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    """Base provisioning domain error."""


class ProvisioningConflictError(ProvisioningError):
    """Raised when a unique X-UI identity cannot be generated safely."""


class ZeroUsageRefundError(ProvisioningError):
    """Raised when a zero-usage refund request is invalid."""


class MigrationError(ProvisioningError):
    """Raised when migrating a subscription to a different inbound fails."""


@dataclass(slots=True, frozen=True)
class ProvisioningResult:
    subscription: Subscription
    xui_client: XUIClientRecord | None
    vless_uri: str
    sub_link: str


@dataclass(slots=True, frozen=True)
class MigrationResult:
    subscription: Subscription
    xui_client: XUIClientRecord
    new_vless_uri: str
    new_sub_link: str
    old_inbound_label: str
    new_inbound_label: str
    remaining_bytes: int


class ProvisioningManager:
    def __init__(self, session: AsyncSession, xui_client: SanaeiXUIClient | None = None) -> None:
        self.session = session
        self.xui_client = xui_client
        self.wallet_manager = WalletManager(session)

    async def preflight_check_plan(self, plan_id: UUID) -> tuple[bool, str | None]:
        """Quick health probe for the plan's X-UI inbound BEFORE we debit
        the user's wallet.

        Returns (ok, reason). When ok is False, ``reason`` is a Persian
        string suitable for showing the user — caller should refuse the
        purchase and NOT debit anything.

        Implementation: load the plan's inbound + server + credentials,
        then attempt a fast login() against the panel. We deliberately
        keep the timeout small; a healthy panel responds in well under a
        second.
        """
        plan = await self.session.scalar(
            select(Plan)
            .options(
                selectinload(Plan.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials)
            )
            .where(Plan.id == plan_id)
        )
        if plan is None or not plan.is_active:
            return False, "این پلن در حال حاضر فعال نیست."

        # Ready-config pools don't need an X-UI panel at purchase time —
        # the configs live in a static pool. Skip the panel probe.
        ready_pool = await self.session.scalar(
            select(ReadyConfigPool).where(
                ReadyConfigPool.plan_id == plan.id,
                ReadyConfigPool.is_active.is_(True),
            )
        )
        if ready_pool is not None:
            return True, None

        inbound = plan.inbound
        if inbound is None:
            # Same fallback the provisioning path uses.
            inbound = await self.session.scalar(
                select(XUIInboundRecord)
                .options(selectinload(XUIInboundRecord.server).selectinload(XUIServerRecord.credentials))
                .where(XUIInboundRecord.is_active.is_(True))
                .order_by(XUIInboundRecord.created_at.asc())
                .limit(1)
            )
        if inbound is None or not inbound.is_active:
            return False, "هیچ سرور فعالی برای این پلن در دسترس نیست."
        try:
            server = ensure_inbound_server_loaded(inbound)
        except Exception:
            return False, "اطلاعات سرور این پلن ناقص است."
        if not server.is_active:
            return False, "سرور این پلن غیرفعال شده است."
        if server.credentials is None:
            return False, "اطلاعات ورود به سرور موجود نیست."

        try:
            async with self._get_xui_client_for_server(server) as xui_client:
                await xui_client.login()
            return True, None
        except Exception as exc:
            logger.warning(
                "preflight_check_plan: panel %s unreachable (%s): %s",
                server.name, type(exc).__name__, exc,
            )
            return False, (
                "اتصال به سرور موقتاً برقرار نشد. لطفاً چند دقیقه‌ی دیگر "
                "دوباره تلاش کنید — وجهی از حساب شما کم نشد."
            )

    async def preflight_check_subscription(self, subscription_id: UUID) -> tuple[bool, str | None]:
        """Same probe but for an existing subscription (renewal path)."""
        sub = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.xui_client)
                .selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials)
            )
            .where(Subscription.id == subscription_id)
        )
        if sub is None:
            return False, "سرویس یافت نشد."
        if sub.xui_client is None or sub.xui_client.inbound is None:
            return True, None  # ready-config — no panel involved
        try:
            server = ensure_inbound_server_loaded(sub.xui_client.inbound)
        except Exception:
            return False, "اطلاعات سرور این سرویس ناقص است."
        if not server.is_active or server.credentials is None:
            return False, "سرور این سرویس در حال حاضر در دسترس نیست."
        try:
            async with self._get_xui_client_for_server(server) as xui_client:
                await xui_client.login()
            return True, None
        except Exception as exc:
            logger.warning(
                "preflight_check_subscription: panel %s unreachable (%s): %s",
                server.name, type(exc).__name__, exc,
            )
            return False, (
                "اتصال به سرور موقتاً برقرار نشد. لطفاً چند دقیقه‌ی دیگر "
                "دوباره تلاش کنید — وجهی از حساب شما کم نشد."
            )

    async def provision_subscription(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        order_id: UUID,
        config_name: str = "VPN",
    ) -> ProvisioningResult:
        # Load plan WITH its inbound relation
        plan = await self.session.scalar(
            select(Plan)
            .options(
                selectinload(Plan.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials)
            )
            .where(Plan.id == plan_id)
        )
        if plan is None or not plan.is_active:
            raise ProvisioningError("Plan not found or inactive.")

        order = await self.session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            raise ProvisioningError("Order not found for user.")

        stock_reserved = False

        ready_pool = await self.session.scalar(
            select(ReadyConfigPool).where(
                ReadyConfigPool.plan_id == plan.id,
                ReadyConfigPool.is_active.is_(True),
            )
        )
        if ready_pool is not None:
            try:
                stock_reserved = await reserve_plan_sale(self.session, plan.id)
                return await self._provision_ready_config(
                    user_id=user_id,
                    plan=plan,
                    order=order,
                    pool=ready_pool,
                )
            except PlanStockError as exc:
                raise ProvisioningError("Plan stock is sold out.") from exc
            except Exception:
                if stock_reserved:
                    await release_plan_sale(self.session, plan.id)
                raise

        # Use the plan's specific inbound instead of random selection
        inbound: XUIInboundRecord | None = plan.inbound

        if inbound is None:
            # Fallback: try to find any active inbound (legacy plans without inbound_id)
            inbound = await self.session.scalar(
                select(XUIInboundRecord)
                .options(selectinload(XUIInboundRecord.server).selectinload(XUIServerRecord.credentials))
                .where(XUIInboundRecord.is_active.is_(True))
                .order_by(XUIInboundRecord.created_at.asc())
                .limit(1)
            )

        if inbound is None:
            raise ProvisioningError(
                "هیچ اینباند فعالی برای ساخت کانفیگ موجود نیست. "
                "ابتدا یک سرور اضافه کنید."
            )

        if not inbound.is_active:
            raise ProvisioningError("The selected inbound is inactive.")

        server = ensure_inbound_server_loaded(inbound)
        if not server.is_active:
            raise ProvisioningError("The selected server is inactive.")

        if server.max_clients is not None:
            active_count = await self.session.scalar(
                select(func.count())
                .select_from(XUIClientRecord)
                .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
                .where(
                    XUIClientRecord.is_active.is_(True),
                    XUIInboundRecord.server_id == server.id
                )
            ) or 0
            if active_count >= server.max_clients:
                raise ProvisioningError("ظرفیت این سرور تکمیل شده است. لطفاً به پشتیبانی اطلاع دهید.")

        client_uuid, _username, _email, sub_id = await self._generate_unique_client_identity()
        # Use config_name as the display name in X-UI panel
        email = f"{config_name}_{sub_id[:6]}"
        now = datetime.now(timezone.utc)
        # first_use mode: expiryTime=0 means unlimited until activation
        # The sync job will set the real expiry when the user first connects
        expiry_ms = 0
        sub_link = build_sub_link(server, sub_id)
        security_settings = await AppSettingsRepository(self.session).get_service_security_settings()
        vless_uri = build_vless_uri(
            client_uuid=client_uuid,
            server=server,
            inbound=inbound,
            sub_id=sub_id,
            remark=config_name,
        )

        xui_payload = XUIClient(
            id=client_uuid,
            uuid=client_uuid,
            email=email,
            limitIp=plan.effective_ip_limit(security_settings.xui_limit_ip),
            totalGB=plan.volume_bytes,
            expiryTime=expiry_ms,
            enable=True,
            subId=sub_id,
            comment=f"user:{user_id};order:{order_id}",
        )

        logger.info(
            "Provisioning config: inbound_remote_id=%s, protocol=%s, email=%s, config_name=%s",
            inbound.xui_inbound_remote_id,
            inbound.protocol,
            email,
            config_name,
        )

        stock_reserved = False
        xui_call_succeeded = False
        try:
            stock_reserved = await reserve_plan_sale(self.session, plan.id)

            # Savepoint: write DB rows first, then call X-UI. If X-UI fails,
            # the savepoint rolls back so we never end up with rows pointing
            # at a non-existent panel client. If the DB flush fails after a
            # successful X-UI call, we issue a compensating delete on the
            # panel below.
            async with self.session.begin_nested():
                subscription = Subscription(
                    user_id=user_id,
                    order_id=order_id,
                    plan_id=plan_id,
                    status="pending_activation",
                    activation_mode="first_use",
                    starts_at=None,
                    ends_at=None,
                    activated_at=None,
                    expired_at=None,
                    volume_bytes=plan.volume_bytes,
                    used_bytes=0,
                    sub_link=sub_link,
                )
                self.session.add(subscription)
                await self.session.flush()

                xui_record = XUIClientRecord(
                    subscription_id=subscription.id,
                    inbound_id=inbound.id,
                    xui_client_remote_id=client_uuid,
                    email=email,
                    client_uuid=client_uuid,
                    username=config_name,
                    sub_link=sub_link,
                    usage_bytes=0,
                    is_active=True,
                )
                self.session.add(xui_record)

                order.status = "provisioned"
                await self.session.flush()

                async with self._get_xui_client_for_server(server) as xui_client:
                    await xui_client.add_client_to_inbound(
                        inbound.xui_inbound_remote_id, xui_payload
                    )
                xui_call_succeeded = True

            await self.session.refresh(subscription)
            await self.session.refresh(xui_record)
        except PlanStockError as exc:
            raise ProvisioningError("Plan stock is sold out.") from exc
        except Exception:
            if stock_reserved:
                await release_plan_sale(self.session, plan.id)
            if xui_call_succeeded:
                # X-UI created the client but the surrounding work failed —
                # try to delete it so the panel doesn't keep an orphan.
                try:
                    async with self._get_xui_client_for_server(server) as xui_client:
                        await xui_client.delete_client(
                            inbound_id=inbound.xui_inbound_remote_id,
                            client_id=client_uuid,
                        )
                except Exception as cleanup_exc:
                    logger.error(
                        "Failed to compensate orphan X-UI client %s on inbound %s: %s",
                        client_uuid, inbound.xui_inbound_remote_id, cleanup_exc,
                    )
            raise
        return ProvisioningResult(
            subscription=subscription,
            xui_client=xui_record,
            vless_uri=vless_uri,
            sub_link=sub_link,
        )

    async def _provision_ready_config(
        self,
        *,
        user_id: UUID,
        plan: Plan,
        order: Order,
        pool: ReadyConfigPool,
    ) -> ProvisioningResult:
        item = await self.session.scalar(
            select(ReadyConfigItem)
            .where(
                ReadyConfigItem.pool_id == pool.id,
                ReadyConfigItem.status == "available",
            )
            .order_by(ReadyConfigItem.created_at.asc(), ReadyConfigItem.line_number.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            raise ProvisioningError("Ready config stock is empty for this plan.")

        now = datetime.now(timezone.utc)
        subscription = Subscription(
            user_id=user_id,
            order_id=order.id,
            plan_id=plan.id,
            status="pending_activation",
            activation_mode="first_use",
            starts_at=None,
            ends_at=None,
            activated_at=None,
            expired_at=None,
            volume_bytes=plan.volume_bytes,
            used_bytes=0,
            sub_link="",  # We will update this after flush
        )
        self.session.add(subscription)
        await self.session.flush()

        from core.config import settings
        from core.miniapp_auth import sign_subscription_id

        # Check if the content has a custom sub link separated by '|'
        parts = item.content.split("|", 1)
        vless_uri = parts[0].strip()
        custom_sub_link = parts[1].strip() if len(parts) > 1 else None

        sub_sig = sign_subscription_id(str(subscription.id))
        bot_sub_link = f"{settings.web_base_url.rstrip('/')}/api/sub/{subscription.id}?sig={sub_sig}"
        
        # If the admin provided a custom sub link, use it. Otherwise fallback to bot sub link.
        final_sub_link = custom_sub_link if custom_sub_link else bot_sub_link
        
        subscription.sub_link = final_sub_link

        item.status = "sold"
        item.assigned_user_id = user_id
        item.order_id = order.id
        item.subscription_id = subscription.id
        item.sold_at = now
        order.status = "provisioned"

        await self.session.flush()
        await self.session.refresh(subscription)
        return ProvisioningResult(
            subscription=subscription,
            xui_client=None,
            vless_uri=vless_uri,
            sub_link=final_sub_link,
        )

    async def process_zero_usage_refund(
        self,
        *,
        subscription_id: UUID,
        user_id: UUID,
    ) -> Subscription:
        subscription = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.order),
                selectinload(Subscription.plan),
                selectinload(Subscription.xui_client)
                .selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials),
            )
            .where(
                Subscription.id == subscription_id,
                Subscription.user_id == user_id,
            )
            .with_for_update()
        )
        if subscription is None:
            raise ZeroUsageRefundError("Subscription not found.")
        if subscription.status != "pending_activation":
            raise ZeroUsageRefundError("Only pending_activation subscriptions can be refunded.")
        if subscription.used_bytes != 0:
            raise ZeroUsageRefundError("Subscription has usage and is not eligible for zero-usage refund.")
        if subscription.order is None or subscription.plan is None:
            raise ZeroUsageRefundError("Subscription is missing order or plan references.")

        # Block refund for ready configs — they are pre-made and cannot be returned
        from models.ready_config import ReadyConfigItem
        is_ready_config = await self.session.scalar(
            select(ReadyConfigItem.id).where(ReadyConfigItem.subscription_id == subscription.id)
        )
        if is_ready_config:
            raise ZeroUsageRefundError("Ready configs are not eligible for refund.")

        xui_record = subscription.xui_client
        if xui_record is not None:
            await self._disable_xui_client(
                xui_record=xui_record,
                volume_bytes=subscription.volume_bytes,
                ends_at=None,
            )
            xui_record.is_active = False

        refund_amount = Decimal(str(subscription.order.amount))
        await self.wallet_manager.process_transaction(
            user_id=user_id,
            amount=refund_amount,
            transaction_type="refund",
            direction="credit",
            currency=subscription.order.currency,
            reference_type="order",
            reference_id=subscription.order.id,
            description="Zero-usage refund",
            metadata={"subscription_id": str(subscription.id)},
        )

        subscription.status = "refunded"
        subscription.expired_at = datetime.now(timezone.utc)
        subscription.order.status = "refunded"

        await self.session.flush()
        return subscription

    async def _generate_unique_client_identity(self) -> tuple[str, str, str, str]:
        for _ in range(10):
            client_uuid = str(uuid4())
            username = f"u_{client_uuid.replace('-', '')[:12]}"
            email = f"{username}@tg.local"
            sub_id = uuid4().hex[:16]

            exists = await self.session.scalar(
                select(XUIClientRecord).where(
                    (XUIClientRecord.client_uuid == client_uuid)
                    | (XUIClientRecord.username == username)
                    | (XUIClientRecord.email == email)
                )
            )
            if exists is None:
                return client_uuid, username, email, sub_id

        raise ProvisioningConflictError("Could not generate a unique X-UI client identity.")

    # ── Migration: move a subscription's X-UI client to a different inbound ──
    #
    # The user-facing case: their config stopped routing on the original
    # inbound (DPI block, server overloaded, etc.) and the admin has set up
    # a fresh inbound they can switch to without losing remaining volume.
    #
    # Strategy: do every DB write inside a SAVEPOINT and call X-UI on the
    # target inbound last. If the X-UI call fails, the savepoint rolls back
    # and the DB is untouched. After the savepoint commits we issue a
    # best-effort delete on the OLD panel — if that fails the orphan can be
    # cleaned up by the admin later, but it never makes the user's record
    # inconsistent.
    async def migrate_subscription_to_inbound(
        self,
        *,
        subscription_id: UUID,
        target_inbound_id: UUID,
    ) -> MigrationResult:
        sub = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.plan),
                selectinload(Subscription.xui_client)
                .selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials),
            )
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        if sub is None:
            raise MigrationError("سرویس یافت نشد.")
        if sub.status not in ("active", "pending_activation"):
            raise MigrationError(
                "فقط سرویس‌های فعال یا منتظر فعال‌سازی قابل انتقال هستند."
            )

        xui = sub.xui_client
        if xui is None:
            raise MigrationError("این سرویس کانفیگ X-UI ندارد و قابل انتقال نیست.")
        if xui.inbound_id == target_inbound_id:
            raise MigrationError("این سرویس از قبل روی همین اینباند است.")

        # Load target inbound + server with credentials
        target_inbound = await self.session.scalar(
            select(XUIInboundRecord)
            .options(
                selectinload(XUIInboundRecord.server).selectinload(XUIServerRecord.credentials),
            )
            .where(XUIInboundRecord.id == target_inbound_id)
        )
        if target_inbound is None or not target_inbound.is_active:
            raise MigrationError("اینباند هدف موجود یا فعال نیست.")

        target_server = ensure_inbound_server_loaded(target_inbound)
        if not target_server.is_active:
            raise MigrationError("سرور اینباند هدف فعال نیست.")

        # Capacity check for the target server (same rule as provisioning).
        if target_server.max_clients is not None:
            active_count = await self.session.scalar(
                select(func.count())
                .select_from(XUIClientRecord)
                .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
                .where(
                    XUIClientRecord.is_active.is_(True),
                    XUIInboundRecord.server_id == target_server.id,
                )
            ) or 0
            if active_count >= target_server.max_clients:
                raise MigrationError("ظرفیت سرور هدف تکمیل شده است.")

        remaining_bytes = max(sub.volume_bytes - sub.used_bytes, 0)
        _MIN_USABLE_BYTES = 100 * 1024 * 1024  # 100 MB
        if remaining_bytes < _MIN_USABLE_BYTES:
            raise MigrationError(
                "حجم باقی‌مانده‌ی این سرویس خیلی کم است (کمتر از ۱۰۰ مگابایت). "
                "ابتدا سرویس را تمدید کنید."
            )

        # Generate fresh identity. We must NOT reuse the old uuid/email —
        # the old client still exists on the old panel until cleanup, and
        # X-UI rejects duplicates by uuid within a single panel; even across
        # panels we want a fresh tracking row.
        #
        # IMPORTANT: keep the user-facing display name stable across the
        # migration. The provisioning convention is
        # ``<display>_<sub_id[:6]>`` — we extract the <display> part from
        # the old row, then rotate ONLY the 6-char suffix + the uuid.
        # Without this the row ends up named ``u_a1b2c3d4e5f6`` (the
        # generic identity helper's fallback), which the user sees as
        # their config name in every bot message and in the X-UI panel.
        import re as _re
        old_display_name = (xui.username or "").strip()
        m = _re.match(r"^(.+)_[a-f0-9]{6}$", old_display_name)
        display_root = m.group(1) if m else (old_display_name or None)
        if not display_root or len(display_root) > 50:
            display_root = (sub.plan.name if sub.plan else None) or "config"
        # Keep only safe characters so panel/email validators don't choke.
        display_root = _re.sub(r"[^A-Za-z0-9_\-]+", "", display_root) or "config"

        # Pick a fresh (uuid, sub_id, email/username) tuple that doesn't
        # collide with any OTHER XUIClientRecord. We deliberately exclude
        # this row from the uniqueness check so the old values (which we're
        # about to overwrite) don't make every candidate look duplicated.
        from uuid import uuid4 as _uuid4
        client_uuid: str = ""
        username: str = ""
        email: str = ""
        sub_id: str = ""
        for _attempt in range(20):
            candidate_sub_id = _uuid4().hex[:16]
            candidate_uuid = str(_uuid4())
            candidate_username = f"{display_root}_{candidate_sub_id[:6]}"
            candidate_email = candidate_username
            collision = await self.session.scalar(
                select(XUIClientRecord).where(
                    XUIClientRecord.id != xui.id,
                    (XUIClientRecord.client_uuid == candidate_uuid)
                    | (XUIClientRecord.username == candidate_username)
                    | (XUIClientRecord.email == candidate_email),
                )
            )
            if collision is None:
                client_uuid = candidate_uuid
                sub_id = candidate_sub_id
                username = candidate_username
                email = candidate_email
                break
        if not client_uuid:
            raise MigrationError("نتوانستیم هویت یکتای جدید بسازیم. لطفاً دوباره تلاش کنید.")

        new_sub_link = build_sub_link(target_server, sub_id)
        security_settings = await AppSettingsRepository(self.session).get_service_security_settings()

        # The remark on the new client matches the display root so the
        # user sees the SAME name they originally chose, both in the bot
        # and in the X-UI panel.
        remark = display_root
        new_vless_uri = build_vless_uri(
            client_uuid=client_uuid,
            server=target_server,
            inbound=target_inbound,
            sub_id=sub_id,
            remark=remark,
        )

        # Expiry: if the sub has a concrete ends_at, propagate it. Otherwise
        # leave at 0 so the panel inherits "first-use" semantics from the
        # plan settings.
        expiry_ms = int(sub.ends_at.timestamp() * 1000) if sub.ends_at is not None else 0

        # Inherit per-plan ip_limit when the sub still has a plan attached;
        # otherwise fall back to the global security setting. `sub.plan` is
        # already accessed earlier in this function (display-name lookup),
        # so it's eager-loaded by the caller.
        _plan_for_ip = sub.plan if sub.plan_id else None
        _ip_limit = _plan_for_ip.effective_ip_limit(security_settings.xui_limit_ip) if _plan_for_ip else security_settings.xui_limit_ip
        xui_payload = XUIClient(
            id=client_uuid,
            uuid=client_uuid,
            email=email,
            limitIp=_ip_limit,
            totalGB=remaining_bytes,
            expiryTime=expiry_ms,
            enable=True,
            subId=sub_id,
            comment=f"migrated:sub={sub.id}",
        )

        # Snapshot OLD inbound info BEFORE we mutate the row, so we can do
        # the cleanup delete after the savepoint commits.
        old_inbound = xui.inbound
        old_server = ensure_inbound_server_loaded(old_inbound) if old_inbound else None
        old_inbound_remote_id = old_inbound.xui_inbound_remote_id if old_inbound else None
        old_remote_client_id = xui.xui_client_remote_id or xui.client_uuid
        old_inbound_label = (
            (old_inbound.remark or f"اینباند {old_inbound.xui_inbound_remote_id}")
            if old_inbound else "نامشخص"
        )
        new_inbound_label = (
            target_inbound.remark or f"اینباند {target_inbound.xui_inbound_remote_id}"
        )

        logger.info(
            "Migrating subscription %s: %s -> %s (remaining=%d bytes)",
            sub.id, old_inbound_label, new_inbound_label, remaining_bytes,
        )

        xui_call_succeeded = False
        savepoint_committed = False
        try:
            async with self.session.begin_nested():
                # Apply DB changes first. If the X-UI call fails, savepoint
                # rolls these back automatically.
                xui.inbound_id = target_inbound.id
                xui.client_uuid = client_uuid
                xui.xui_client_remote_id = client_uuid
                xui.email = email
                xui.username = username
                xui.sub_link = new_sub_link
                xui.usage_bytes = 0
                xui.is_active = True

                sub.sub_link = new_sub_link
                # Effectively start a fresh quota on the new panel: the new
                # X-UI client is provisioned with `remaining_bytes`. Keep
                # the DB record consistent so the bot reports the correct
                # numbers until the sync job pulls real usage.
                sub.volume_bytes = remaining_bytes
                # Migration resets the X-UI client's traffic counter to 0,
                # so we must save the pre-migration consumption into the
                # lifetime counter — otherwise reseller billing would
                # silently lose every byte the user consumed before the
                # admin moved them to a new inbound.
                sub.lifetime_used_bytes = (
                    (sub.lifetime_used_bytes or 0) + (sub.used_bytes or 0)
                )
                sub.used_bytes = 0

                await self.session.flush()

                async with self._get_xui_client_for_server(target_server) as xui_api:
                    await xui_api.add_client_to_inbound(
                        target_inbound.xui_inbound_remote_id, xui_payload,
                    )
                xui_call_succeeded = True
            # Exiting the `begin_nested` block commits the savepoint. Past
            # this line we MUST NOT compensate-delete on the target panel,
            # because the DB is now the source of truth pointing at it.
            savepoint_committed = True

            await self.session.refresh(sub)
            await self.session.refresh(xui)
        except MigrationError:
            raise
        except Exception as exc:
            # Compensate ONLY if the new panel client was created AND the
            # savepoint did not commit (i.e. we are about to rollback the
            # DB to its previous state). Otherwise the DB is already
            # pointing at the new client and deleting it would orphan the
            # user.
            if xui_call_succeeded and not savepoint_committed:
                try:
                    async with self._get_xui_client_for_server(target_server) as xui_api:
                        await xui_api.delete_client(
                            inbound_id=target_inbound.xui_inbound_remote_id,
                            client_id=client_uuid,
                        )
                except Exception as cleanup_exc:
                    logger.error(
                        "Migration cleanup failed for client %s on inbound %s: %s",
                        client_uuid, target_inbound.xui_inbound_remote_id, cleanup_exc,
                    )
            raise MigrationError(f"انتقال ناموفق بود: {exc}") from exc

        # Best-effort: remove the old client from the old panel. Failures
        # here are non-fatal — the user already has the new client and a
        # working sub_link.
        if old_server is not None and old_inbound_remote_id is not None:
            try:
                async with self._get_xui_client_for_server(old_server) as xui_api:
                    await xui_api.delete_client(
                        inbound_id=old_inbound_remote_id,
                        client_id=old_remote_client_id,
                    )
                logger.info(
                    "Removed orphan client %s from old inbound %s",
                    old_remote_client_id, old_inbound_remote_id,
                )
            except Exception as exc:
                logger.warning(
                    "Could not delete old client %s on inbound %s: %s",
                    old_remote_client_id, old_inbound_remote_id, exc,
                )

        return MigrationResult(
            subscription=sub,
            xui_client=xui,
            new_vless_uri=new_vless_uri,
            new_sub_link=new_sub_link,
            old_inbound_label=old_inbound_label,
            new_inbound_label=new_inbound_label,
            remaining_bytes=remaining_bytes,
        )

    async def migrate_imported_subscription_to_inbound(
        self,
        *,
        subscription_id: UUID,
        target_inbound_id: UUID,
    ) -> MigrationResult:
        """Move a subscription imported from the legacy bot onto a real X-UI inbound.

        Different from `migrate_subscription_to_inbound` in TWO ways:

        1. There's no existing XUIClientRecord on our side (the original
           lived on the previous operator's panels). We CREATE a fresh
           one rather than mutate.
        2. The remark / username used on the new X-UI panel is taken
           VERBATIM from `Subscription.legacy_remark` — no regex parse,
           no hex suffix, no plan-name fallback. This is the operator's
           hard requirement ("بدون تغییر نام") and what makes imported
           users see exactly the same config name they had before.

        On success the sub flips out of imported mode (source / legacy_*
        cleared) and behaves like a native sub from here on.

        Raises MigrationError on validation issues; raises generic Exception
        on X-UI failures (savepoint rolls everything back).
        """
        # Eager-load `plan` because we touch `sub.plan` further down for
        # the per-plan ip_limit lookup. Without selectinload(), the
        # attribute access lazy-loads, which under async SQLAlchemy raises
        # `greenlet_spawn has not been called` outside of an explicit
        # greenlet context.
        sub = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == subscription_id),
        )
        if sub is None:
            raise MigrationError("کانفیگ پیدا نشد.")
        if sub.source != "imported_legacy":
            raise MigrationError("این عملیات فقط برای کانفیگ‌های وارد‌شده از ربات قبلی است.")
        if not sub.legacy_remark:
            raise MigrationError("نام کانفیگ قدیمی ثبت نشده — انتقال ممکن نیست.")

        # Admin's allowed-target whitelist (same one the regular migration uses).
        # `get_migration_target_inbound_ids` returns `list[str]` — we have to
        # cast each entry to UUID before membership-checking, otherwise the
        # comparison is always `UUID not in [str, str, ...]` → False positive
        # that blocks every imported-sub migration when ANY whitelist exists.
        from repositories.settings import AppSettingsRepository
        settings_repo = AppSettingsRepository(self.session)
        allowed_raw = await settings_repo.get_migration_target_inbound_ids()
        allowed: list[UUID] = []
        for raw in allowed_raw:
            try:
                allowed.append(UUID(raw))
            except (TypeError, ValueError):
                continue
        if allowed and target_inbound_id not in allowed:
            raise MigrationError("این اینباند برای انتقال مجاز نیست.")

        target_inbound = await self.session.scalar(
            select(XUIInboundRecord)
            .options(selectinload(XUIInboundRecord.server).selectinload(XUIServerRecord.credentials))
            .where(XUIInboundRecord.id == target_inbound_id),
        )
        if target_inbound is None or not target_inbound.is_active:
            raise MigrationError("اینباند مقصد در دسترس نیست.")
        target_server = ensure_inbound_server_loaded(target_inbound)
        if not target_server.is_active:
            raise MigrationError("سرور مقصد غیرفعال است.")

        # Fresh identity for the new X-UI client.
        #
        # User-visible NAME (the vless URI's `#…` fragment) is `remark` —
        # this MUST stay byte-for-byte identical to `legacy_remark` so the
        # imported user sees the same config name they had before
        # ("بدون تغییر نام" — the operator's hard requirement).
        #
        # X-UI's `email` field, however, is an INTERNAL identifier that the
        # user never sees. If the target panel already has a client with
        # that email (typical when a previous migration attempt half-
        # succeeded, or when the legacy panel and target panel happen to
        # share the same email convention), X-UI rejects with
        # "Duplicate email: …". We retry with a short hex suffix on the
        # email — `remark` stays unchanged, so the user's display name is
        # preserved.
        client_uuid = str(uuid4())
        sub_id = secrets.token_hex(8)
        remark = sub.legacy_remark
        username = remark  # mirrors `remark` for our internal records

        # Quota that "carries over" from the legacy bot. We have no
        # usage history on our side; the volume the user purchased on
        # the legacy bot is what we provision again. lifetime_used_bytes
        # stays at 0 — we never saw the bytes deliver.
        remaining_bytes = max(int(sub.volume_bytes or 0), 0)

        new_sub_link = build_sub_link(target_server, sub_id)
        new_vless_uri = build_vless_uri(
            client_uuid=client_uuid,
            server=target_server,
            inbound=target_inbound,
            sub_id=sub_id,
            remark=remark,
        )

        security_settings = await settings_repo.get_service_security_settings()
        expiry_ms = int(sub.ends_at.timestamp() * 1000) if sub.ends_at is not None else 0

        # Per-plan IP cap if available; otherwise global. Imported subs
        # often don't have a plan, so the global default is the typical path.
        _plan_for_ip = sub.plan if sub.plan_id else None
        _ip_limit = _plan_for_ip.effective_ip_limit(security_settings.xui_limit_ip) if _plan_for_ip else security_settings.xui_limit_ip

        new_inbound_label = (
            target_inbound.remark or f"اینباند {target_inbound.xui_inbound_remote_id}"
        )

        logger.info(
            "Migrating imported subscription %s to inbound %s — remark=%r",
            sub.id, new_inbound_label, remark,
        )

        # Retry on "Duplicate email" with a hex-suffixed email. First
        # attempt is verbatim; subsequent attempts suffix the email only.
        # `remark` (and so the user-visible name) is never modified.
        email_candidates: list[str] = [remark]
        for _ in range(5):
            email_candidates.append(f"{remark}_{secrets.token_hex(2)}")

        xui_call_succeeded = False
        savepoint_committed = False
        xui = None
        last_dup_exc: Exception | None = None

        # ── Recover volume + expiry + UUID from the target X-UI panel ──
        #
        # The legacy bot's import dropped the volume column on the floor
        # (different schemas across forks), so `sub.volume_bytes` is 0
        # for most imported subs. But the ORIGINAL client probably still
        # exists on the target inbound (same panel the legacy operator
        # used). We look it up by email==legacy_remark and pull its
        # `totalGB` + `expiryTime` so the migrated config keeps the user's
        # real quota — AND we delete the original on the panel so the
        # operator doesn't end up with two clients sharing one identity.
        #
        # Best-effort: if any step fails, we fall back to creating a
        # fresh client with the existing (possibly zero) volume. The
        # retry-with-suffix loop below still handles Duplicate email.
        async def _adopt_existing_client_if_any() -> tuple[int | None, int | None, str | None]:
            """Returns (recovered_total_bytes, recovered_expiry_ms, deleted_uuid).

            Whole call is wrapped in a 25-second asyncio timeout so a slow or
            hung X-UI panel doesn't block the migration indefinitely. All
            steps log loudly so silent hangs become visible in the bot logs.
            """
            import asyncio as _asyncio
            import json as _json
            target_remote_id = target_inbound.xui_inbound_remote_id

            async def _run() -> tuple[int | None, int | None, str | None]:
                logger.info("[ADOPT] opening X-UI session for %s", target_server.name)
                async with self._get_xui_client_for_server(target_server) as xui_api:
                    logger.info("[ADOPT] listing inbounds...")
                    inbounds_list = await xui_api.get_inbounds()
                    logger.info("[ADOPT] got %d inbounds", len(inbounds_list))

                    existing: dict | None = None
                    for ib in inbounds_list:
                        if ib.id != target_remote_id:
                            continue
                        ib_settings = ib.settings or {}
                        if isinstance(ib_settings, str):
                            try:
                                ib_settings = _json.loads(ib_settings)
                            except Exception:
                                ib_settings = {}
                        clients = ib_settings.get("clients") or []
                        logger.info("[ADOPT] target inbound has %d clients — searching for email=%r", len(clients), remark)
                        for client in clients:
                            if str(client.get("email") or "") == remark:
                                existing = client
                                break
                        break

                    if not existing:
                        logger.info("[ADOPT] no existing client with that email — fresh migration path")
                        return None, None, None

                    recovered_total = existing.get("totalGB")
                    recovered_expiry = existing.get("expiryTime")
                    client_id_to_delete = (
                        existing.get("id") or existing.get("uuid") or existing.get("email")
                    )
                    logger.info(
                        "[ADOPT] found existing client — totalGB=%s expiryTime=%s id=%s",
                        recovered_total, recovered_expiry, client_id_to_delete,
                    )

                    if client_id_to_delete:
                        logger.info("[ADOPT] deleting existing client %s", client_id_to_delete)
                        try:
                            await xui_api.delete_client(
                                inbound_id=target_remote_id,
                                client_id=str(client_id_to_delete),
                            )
                            logger.info("[ADOPT] deleted existing client %s", client_id_to_delete)
                        except Exception as del_exc:
                            logger.warning(
                                "[ADOPT] could not delete existing client %s — falling back to suffix retry: %s",
                                client_id_to_delete, del_exc,
                            )
                            client_id_to_delete = None
                return (
                    int(recovered_total) if recovered_total is not None else None,
                    int(recovered_expiry) if recovered_expiry is not None else None,
                    client_id_to_delete,
                )

            try:
                return await _asyncio.wait_for(_run(), timeout=25.0)
            except _asyncio.TimeoutError:
                logger.warning(
                    "[ADOPT] X-UI lookup/delete timed out after 25 s — falling through to fresh migration"
                )
                return None, None, None
            except Exception as exc:
                logger.warning(
                    "[ADOPT] X-UI lookup/delete failed (%s: %s) — falling through to fresh migration",
                    type(exc).__name__, exc,
                )
                return None, None, None

        recovered_total, recovered_expiry, deleted_existing_uuid = await _adopt_existing_client_if_any()

        if recovered_total is not None and recovered_total > 0 and remaining_bytes == 0:
            # Recovered the real quota from the panel — this is what the
            # user actually has, NOT "unlimited".
            remaining_bytes = recovered_total
            sub.volume_bytes = recovered_total
            logger.info(
                "Imported-migration: recovered volume %d bytes from X-UI panel",
                recovered_total,
            )
        if recovered_expiry is not None and recovered_expiry > 0 and sub.ends_at is None:
            from datetime import datetime as _dt, timezone as _tz
            try:
                sub.ends_at = _dt.fromtimestamp(recovered_expiry / 1000, tz=_tz.utc)
                expiry_ms = recovered_expiry
                logger.info(
                    "Imported-migration: recovered expiry %d (ms) from X-UI panel",
                    recovered_expiry,
                )
            except (OSError, OverflowError, ValueError):
                pass

        # Snapshot every `sub` column we read inside the retry loop, so
        # we never have to access them again after a savepoint rollback
        # expires the object. Without this, the next iteration's
        # `if sub.status == "expired"` triggers an async lazy-load that
        # blows up with `greenlet_spawn has not been called`.
        sub_id_str = sub.id
        sub_was_expired = (sub.status == "expired")

        try:
            for attempt_idx, attempt_email in enumerate(email_candidates):
                # After a failed attempt the savepoint rolled back; any
                # attribute we touch on `sub` after that is in expired
                # state and will async-lazy-load. Refresh once so the
                # column writes inside the next savepoint are clean.
                if attempt_idx > 0:
                    await self.session.refresh(sub)

                attempt_payload = XUIClient(
                    id=client_uuid,
                    uuid=client_uuid,
                    email=attempt_email,
                    limitIp=_ip_limit,
                    totalGB=remaining_bytes,
                    expiryTime=expiry_ms,
                    enable=True,
                    subId=sub_id,
                    comment=f"imported-migrated:sub={sub_id_str}",
                )

                try:
                    async with self.session.begin_nested():
                        # Create the new XUIClientRecord first; if the X-UI
                        # panel call below fails, the savepoint rolls it back.
                        xui = XUIClientRecord(
                            subscription_id=sub.id,
                            inbound_id=target_inbound.id,
                            client_uuid=client_uuid,
                            xui_client_remote_id=client_uuid,
                            email=attempt_email,
                            username=username,
                            sub_link=new_sub_link,
                            is_active=True,
                            usage_bytes=0,
                        )
                        self.session.add(xui)

                        # Flip the sub out of imported mode. Keep
                        # `lifetime_used_bytes` at whatever it was (probably
                        # 0) — there are no pre-migration bytes we can
                        # attribute on our side.
                        sub.sub_link = new_sub_link
                        sub.source = None
                        sub.legacy_link = None
                        # We DO keep `legacy_remark` set deliberately — it
                        # documents the original name forever in case the
                        # operator audits.
                        # Use the pre-loop snapshot here: reading sub.status
                        # mid-loop would lazy-load an expired column after a
                        # prior savepoint rollback.
                        if sub_was_expired:
                            # Migration of an expired imported sub effectively
                            # re-provisions; flip back to active so the user
                            # can use it.
                            sub.status = "active"
                            sub.expired_at = None

                        await self.session.flush()

                        async with self._get_xui_client_for_server(target_server) as xui_api:
                            await xui_api.add_client_to_inbound(
                                target_inbound.xui_inbound_remote_id, attempt_payload,
                            )
                        xui_call_succeeded = True
                    savepoint_committed = True
                    if attempt_idx > 0:
                        logger.info(
                            "Imported-migration succeeded on attempt #%d with email=%r",
                            attempt_idx + 1, attempt_email,
                        )
                    break  # success — exit the retry loop
                except Exception as inner_exc:
                    exc_text = str(inner_exc)
                    is_duplicate = (
                        "Duplicate email" in exc_text
                        or "duplicate email" in exc_text.lower()
                        or "email already" in exc_text.lower()
                    )
                    if is_duplicate and attempt_idx < len(email_candidates) - 1:
                        last_dup_exc = inner_exc
                        logger.warning(
                            "X-UI rejected email=%r as duplicate, retrying with suffix…",
                            attempt_email,
                        )
                        xui_call_succeeded = False  # savepoint rolled it back
                        xui = None
                        continue
                    raise  # non-duplicate, or out of retries — let outer wrap it

            if not savepoint_committed:
                raise MigrationError(
                    "نتوانستیم نام یکتا روی پنل مقصد بسازیم — لطفاً با پشتیبانی تماس بگیرید."
                ) from last_dup_exc

            await self.session.refresh(sub)
            if xui is not None:
                await self.session.refresh(xui)
        except MigrationError:
            raise
        except Exception as exc:
            # If we managed to call add_client successfully but the savepoint
            # rolled back for any reason after that, clean up the panel-side
            # client so we don't leave an orphan.
            if xui_call_succeeded and not savepoint_committed:
                try:
                    async with self._get_xui_client_for_server(target_server) as xui_api:
                        await xui_api.delete_client(
                            inbound_id=target_inbound.xui_inbound_remote_id,
                            client_id=client_uuid,
                        )
                except Exception as cleanup_exc:
                    logger.error(
                        "Imported-migration cleanup failed for client %s on inbound %s: %s",
                        client_uuid, target_inbound.xui_inbound_remote_id, cleanup_exc,
                    )
            raise MigrationError(f"انتقال ناموفق بود: {exc}") from exc

        return MigrationResult(
            subscription=sub,
            xui_client=xui,
            new_vless_uri=new_vless_uri,
            new_sub_link=new_sub_link,
            old_inbound_label="(ربات قبلی)",
            new_inbound_label=new_inbound_label,
            remaining_bytes=remaining_bytes,
        )

    async def _disable_xui_client(
        self,
        *,
        xui_record: XUIClientRecord,
        volume_bytes: int,
        ends_at: datetime | None,
    ) -> None:
        inbound = xui_record.inbound
        if inbound is None:
            raise ProvisioningError("X-UI inbound mapping is missing.")

        server = ensure_inbound_server_loaded(inbound)
        expiry_ms = int(ends_at.timestamp() * 1000) if ends_at is not None else 0
        # Preserve the per-plan ip_limit even on disable so re-enabling
        # the client doesn't silently drop back to the global default.
        global_ip_limit = (await AppSettingsRepository(self.session).get_service_security_settings()).xui_limit_ip
        plan_for_ip = await self.session.scalar(
            select(Plan).join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.id == xui_record.subscription_id)
        )
        effective_ip_limit = plan_for_ip.effective_ip_limit(global_ip_limit) if plan_for_ip else global_ip_limit
        disabled_client = XUIClient(
            id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            uuid=xui_record.client_uuid,
            email=xui_record.email,
            limitIp=effective_ip_limit,
            totalGB=volume_bytes,
            expiryTime=expiry_ms,
            enable=False,
            comment=f"disabled:{xui_record.subscription_id}",
        )
        async with self._get_xui_client_for_server(server) as xui_client:
            await xui_client.update_client(
                inbound_id=inbound.xui_inbound_remote_id,
                client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                client=disabled_client,
            )

    def _get_xui_client_for_server(self, server: XUIServerRecord) -> "_StaticAsyncClientContext":
        if self.xui_client is not None:
            return _StaticAsyncClientContext(self.xui_client)
        return _StaticAsyncClientContext.from_factory(server)


class _StaticAsyncClientContext:
    def __init__(
        self,
        client: SanaeiXUIClient | None = None,
        *,
        server: XUIServerRecord | None = None,
    ) -> None:
        self._client = client
        self._server = server
        self._factory_context = None

    @classmethod
    def from_factory(cls, server: XUIServerRecord) -> "_StaticAsyncClientContext":
        return cls(server=server)

    async def __aenter__(self) -> SanaeiXUIClient:
        if self._client is not None:
            return self._client
        if self._server is None:
            raise ProvisioningError("X-UI server context is missing.")
        self._factory_context = create_xui_client_for_server(self._server)
        return await self._factory_context.__aenter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._factory_context is not None:
            await self._factory_context.__aexit__(exc_type, exc, tb)
