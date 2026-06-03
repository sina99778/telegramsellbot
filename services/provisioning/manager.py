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
from services.panels.adapter import is_pasarguard, record_is_pasarguard
from services.wallet.manager import WalletManager
from services.xui.client import SanaeiXUIClient
from services.xui.runtime import build_sub_link, build_vless_uri, create_xui_client_for_server, ensure_inbound_server_loaded
from services.pasarguard.runtime import create_pasarguard_client_for_server
from schemas.internal.pasarguard import PGUserCreate, PGUserModify


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

        if is_pasarguard(server):
            try:
                async with create_pasarguard_client_for_server(server) as client:
                    await client.get_current_admin()
                return True, None
            except Exception as exc:  # noqa: BLE001
                logger.warning("preflight_check_plan: PG panel %s unreachable: %s", server.name, exc)
                return False, (
                    "اتصال به سرور موقتاً برقرار نشد. لطفاً چند دقیقه‌ی دیگر "
                    "دوباره تلاش کنید — وجهی از حساب شما کم نشد."
                )

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

        if is_pasarguard(server):
            try:
                async with create_pasarguard_client_for_server(server) as client:
                    await client.get_current_admin()
                return True, None
            except Exception as exc:  # noqa: BLE001
                logger.warning("preflight_check_subscription: PG panel %s unreachable: %s", server.name, exc)
                return False, (
                    "اتصال به سرور موقتاً برقرار نشد. لطفاً چند دقیقه‌ی دیگر "
                    "دوباره تلاش کنید — وجهی از حساب شما کم نشد."
                )

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

        # PasarGuard servers provision via the user-centric API (own path).
        if is_pasarguard(server):
            return await self._provision_pasarguard(
                user_id=user_id,
                plan=plan,
                order=order,
                inbound=inbound,
                server=server,
                config_name=config_name,
            )

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

    async def _generate_unique_pg_username(self, config_name: str) -> str:
        """A PasarGuard-valid username ([a-z0-9_], 3-32) unique in OUR DB.
        High entropy (8 hex) so a panel-side collision is astronomically
        unlikely; we also guard our own UNIQUE columns."""
        import re

        base = re.sub(r"[^a-z0-9_]", "", (config_name or "vpn").lower())
        base = base[:20] or "vpn"
        if not base[0].isalpha():
            base = ("u" + base)[:20]
        for _ in range(12):
            candidate = f"{base}_{secrets.token_hex(4)}"
            exists = await self.session.scalar(
                select(XUIClientRecord.id).where(
                    (XUIClientRecord.panel_username == candidate)
                    | (XUIClientRecord.username == candidate)
                    | (XUIClientRecord.email == candidate)
                )
            )
            if exists is None:
                return candidate
        raise ProvisioningConflictError("Could not generate a unique PasarGuard username.")

    async def _provision_pasarguard(
        self,
        *,
        user_id: UUID,
        plan: Plan,
        order: Order,
        inbound: XUIInboundRecord,
        server: XUIServerRecord,
        config_name: str,
    ) -> ProvisioningResult:
        """Provision a config on a PasarGuard panel.

        Mirrors provision_subscription's savepoint discipline: reserve stock →
        write DB rows → call the panel → fill sub_link from the panel response.
        A panel failure rolls back the savepoint; a post-success DB failure
        triggers a compensating delete_user so the panel keeps no orphan.

        first_use activation maps to PasarGuard `on_hold`: the timer starts when
        the user first connects, and the usage-sync job reads the panel's
        status/expire to flip our Subscription to active.
        """
        pg_username = await self._generate_unique_pg_username(config_name)
        duration_days = int(plan.duration_days or 0)
        data_limit = int(plan.volume_bytes) or None  # 0 => unlimited on the panel
        group_id = int(inbound.xui_inbound_remote_id)

        if duration_days > 0:
            create_payload = PGUserCreate(
                username=pg_username,
                status="on_hold",
                data_limit=data_limit,
                group_ids=[group_id],
                on_hold_expire_duration=duration_days * 86400,
                note=f"user:{user_id};order:{order.id}",
            )
        else:
            # Unlimited-duration plan — nothing to hold; activate immediately.
            create_payload = PGUserCreate(
                username=pg_username,
                status="active",
                data_limit=data_limit,
                group_ids=[group_id],
                note=f"user:{user_id};order:{order.id}",
            )

        stock_reserved = False
        pg_created = False
        try:
            stock_reserved = await reserve_plan_sale(self.session, plan.id)

            async with self.session.begin_nested():
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
                    sub_link=None,
                )
                self.session.add(subscription)
                await self.session.flush()

                xui_record = XUIClientRecord(
                    subscription_id=subscription.id,
                    inbound_id=inbound.id,
                    panel_kind="pasarguard",
                    panel_username=pg_username,
                    xui_client_remote_id=None,
                    # Mirror the PG username into email/username (both UNIQUE) so
                    # the generic config views keep working. client_uuid is a
                    # throwaway uuid4 to satisfy the NOT-NULL/UNIQUE column — PG
                    # has no per-config UUID and it is never sent to the panel.
                    email=pg_username,
                    client_uuid=str(uuid4()),
                    username=pg_username,
                    sub_link=None,
                    usage_bytes=0,
                    is_active=True,
                )
                self.session.add(xui_record)
                order.status = "provisioned"
                await self.session.flush()

                async with create_pasarguard_client_for_server(server) as client:
                    pg_user = await client.create_user(create_payload)
                pg_created = True

                sub_link = pg_user.absolute_subscription_url(server.base_url)
                subscription.sub_link = sub_link
                xui_record.sub_link = sub_link
                if pg_user.id is not None:
                    xui_record.xui_client_remote_id = str(pg_user.id)
                await self.session.flush()

            await self.session.refresh(subscription)
            await self.session.refresh(xui_record)
        except PlanStockError as exc:
            raise ProvisioningError("Plan stock is sold out.") from exc
        except Exception:
            if stock_reserved:
                await release_plan_sale(self.session, plan.id)
            if pg_created:
                try:
                    async with create_pasarguard_client_for_server(server) as client:
                        await client.delete_user(pg_username)
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.error(
                        "Failed to compensate orphan PasarGuard user %s: %s",
                        pg_username, cleanup_exc,
                    )
            raise

        return ProvisioningResult(
            subscription=subscription,
            xui_client=xui_record,
            vless_uri="",  # PasarGuard is subscription-URL based; no direct URI
            sub_link=subscription.sub_link or "",
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
        if record_is_pasarguard(xui):
            raise MigrationError("تغییر سرور برای کانفیگ‌های PasarGuard فعلاً پشتیبانی نمی‌شود.")
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

        # Enforce the admin migration whitelist — the SAME rule the imported
        # path and the UI list use. The picker only SHOWS whitelisted inbounds,
        # but aiogram callback_data is plaintext/unsigned, so a crafted confirm
        # callback could otherwise migrate onto a non-whitelisted (but active)
        # inbound. An EMPTY whitelist means "no restriction" (allow any).
        allowed_raw = await AppSettingsRepository(self.session).get_migration_target_inbound_ids()
        allowed_ids: list[UUID] = []
        for raw in allowed_raw:
            try:
                allowed_ids.append(UUID(raw))
            except (TypeError, ValueError):
                continue
        if allowed_ids and target_inbound_id not in allowed_ids:
            raise MigrationError("این اینباند برای انتقال مجاز نیست.")

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
        # FOR UPDATE on the sub row so a double-tapped confirm serializes:
        # the second task blocks here, then re-reads AFTER the first commits
        # (source is None) and falls out via the guard below instead of
        # racing into a second panel client + orphan.
        sub = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.id == subscription_id)
            .with_for_update(),
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
        # Capture the OLD client identity BEFORE the savepoint clears
        # sub.legacy_link — used to delete the stale consolidated client
        # from the panel after the new one is created (so the user doesn't
        # end up with two clients for one config).
        import re as _re_uuid
        _m_old = _re_uuid.match(r"^[a-z0-9]+://([^@]+)@", (sub.legacy_link or "").strip(), _re_uuid.IGNORECASE)
        _old_uuid = _m_old.group(1).strip() if _m_old else None

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

        # ── Recover real volume + expiry (best-effort, bounded) ─────────
        #
        # Imports done before the smart-parser fix stored `volume_bytes=0`.
        # The AUTHORITATIVE fix is re-running the legacy import (the parser
        # now reads the volume column correctly and UPDATEs existing rows).
        #
        # As a lightweight secondary path, if the sub still has an HTTP
        # subscription link we read its standard `subscription-userinfo`
        # header to recover total + expire. This is fully bounded (12s
        # asyncio timeout) and skips vless:// links instantly.
        #
        # We deliberately DO NOT call get_inbounds() on the target panel:
        # on a panel with thousands of clients that response is huge and
        # parsing it synchronously blocks the bot event loop — that was the
        # "bot went silent / locked up" hang the operator reported.
        r_total, r_expire_ms = await self._recover_from_sublink_header(sub)
        if r_total and r_total > 0 and remaining_bytes == 0:
            remaining_bytes = r_total
            sub.volume_bytes = r_total
            logger.info("[SUBINFO] recovered volume %d bytes from sub-link header", r_total)
        if r_expire_ms and r_expire_ms > 0 and sub.ends_at is None:
            from datetime import datetime as _dt2, timezone as _tz2
            try:
                sub.ends_at = _dt2.fromtimestamp(r_expire_ms / 1000, tz=_tz2.utc)
                expiry_ms = r_expire_ms
                logger.info("[SUBINFO] recovered expiry %d (ms) from sub-link header", r_expire_ms)
            except (OSError, OverflowError, ValueError):
                pass

        # Guard: NEVER silently provision an UNLIMITED client for an
        # imported sub whose volume we couldn't determine. On X-UI,
        # totalGB=0 means unlimited — a financial loss for the operator.
        # Point them at the bulk fix (re-import) instead of guessing.
        if remaining_bytes <= 0:
            raise MigrationError(
                "حجم این کانفیگ نامشخص است (۰). برای جلوگیری از ساخت کانفیگ نامحدود، "
                "اول از «پنل مدیریت ← تنظیمات ← ایمپورت دیتابیس ربات قبلی» همان فایل بکاپ را "
                "دوباره بفرست تا حجم همه‌ی کانفیگ‌های واردشده اصلاح شود، بعد دوباره انتقال بزن."
            )

        # ── Subtract ALREADY-CONSUMED volume (critical) ───────────────────
        # `remaining_bytes` is the FULL purchased volume. The user may have
        # already used part of it on their OLD client (same consolidated panel,
        # old inbound). Provisioning the full volume again would hand a user who
        # consumed 10 of 30 GB a fresh 30 GB — free traffic for them, a loss for
        # the operator. Read the old client's up+down (by its email == remark)
        # BEFORE we create the new one, and provision only the remainder. This
        # runs before the migration's stale-client cleanup, so the old client's
        # counters are still present to read.
        old_used_bytes = await self._read_legacy_client_usage(target_server, remark)
        total_purchased_bytes = remaining_bytes
        if old_used_bytes > 0:
            remaining_bytes = total_purchased_bytes - old_used_bytes
            if remaining_bytes <= 0:
                # Fully consumed — keep the client LIMITED (never 0 == unlimited
                # on X-UI) so the user must renew rather than get free traffic.
                remaining_bytes = 1
            logger.info(
                "[MIGRATE-USAGE] sub=%s: legacy client used %d bytes of %d → provisioning remaining=%d",
                sub.id, old_used_bytes, total_purchased_bytes, remaining_bytes,
            )

        # Snapshot every `sub` column we read inside the retry loop, so
        # we never have to access them again after a savepoint rollback
        # expires the object. Without this, the next iteration's
        # `if sub.status == "expired"` triggers an async lazy-load that
        # blows up with `greenlet_spawn has not been called`.
        sub_id_str = sub.id
        sub_was_expired = (sub.status == "expired")
        # Snapshot the RESOLVED volume/expiry (incl. any sub-link-header
        # recovery done above). On a retry, `await self.session.refresh(sub)`
        # reloads these columns from the DB and reverts the recovered values —
        # so we re-apply them inside every savepoint below. Otherwise the panel
        # client gets the right totalGB/expiry but our DB row stays at 0/NULL,
        # and a sub at volume_bytes=0 NEVER volume-expires (the sync job skips
        # the cap when volume_bytes==0) while the bot shows the wrong numbers.
        _target_volume_bytes = remaining_bytes
        _target_ends_at = sub.ends_at

        # A subscription can own AT MOST ONE xui_clients row
        # (uq_xui_clients_subscription_id). Normally an imported sub has
        # none — but a previously half-finished migration, or a row created
        # while reconciling the import against the panel, can leave one
        # behind. Blindly INSERTing a second row then explodes with
        # "duplicate key value violates unique constraint
        # uq_xui_clients_subscription_id". So look for an existing row and
        # REUSE it (mutate in place) instead — exactly like the native
        # migrate path does. This makes the operation safely re-runnable.
        existing_xui = await self.session.scalar(
            select(XUIClientRecord).where(
                XUIClientRecord.subscription_id == sub.id
            )
        )

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
                        # Create the tracking row (or REUSE the pre-existing
                        # one) first; if the X-UI panel call below fails, the
                        # savepoint rolls these changes back.
                        # `username`, `email` AND `client_uuid` each carry a
                        # UNIQUE constraint on xui_clients. The user-visible
                        # name is the vless URI's #fragment (= remark, fixed
                        # above) — these three are INTERNAL ids, so we let the
                        # internal username follow the (possibly suffixed)
                        # email. That way a retry actually dodges BOTH the
                        # email and the username collision at once, without
                        # ever changing what the customer sees.
                        attempt_username = attempt_email[:64]
                        if existing_xui is not None:
                            # Mutate in place — a second INSERT would violate
                            # uq_xui_clients_subscription_id.
                            xui = existing_xui
                            xui.inbound_id = target_inbound.id
                            xui.client_uuid = client_uuid
                            xui.xui_client_remote_id = client_uuid
                            xui.email = attempt_email
                            xui.username = attempt_username
                            xui.sub_link = new_sub_link
                            xui.is_active = True
                            xui.usage_bytes = 0
                        else:
                            xui = XUIClientRecord(
                                subscription_id=sub.id,
                                inbound_id=target_inbound.id,
                                client_uuid=client_uuid,
                                xui_client_remote_id=client_uuid,
                                email=attempt_email,
                                username=attempt_username,
                                sub_link=new_sub_link,
                                is_active=True,
                                usage_bytes=0,
                            )
                            self.session.add(xui)

                        # Flip the sub out of imported mode.
                        sub.sub_link = new_sub_link
                        sub.source = None
                        sub.legacy_link = None
                        # Re-apply the resolved volume/expiry every attempt so a
                        # prior failed attempt's refresh(sub) can't leave the DB
                        # row out of sync with the panel client we just built.
                        sub.volume_bytes = _target_volume_bytes
                        sub.ends_at = _target_ends_at
                        # Carry the legacy consumption into the lifetime counter
                        # (reseller billing) and mark this sub usage-reconciled so
                        # the background backfill job skips it. Computed from the
                        # REFRESHED value each attempt, so a retry doesn't double
                        # it (the prior attempt's add was rolled back).
                        sub.lifetime_used_bytes = (sub.lifetime_used_bytes or 0) + old_used_bytes
                        sub.migration_usage_reconciled = True
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
                    low = exc_text.lower()
                    is_duplicate = (
                        # X-UI panel-side rejection.
                        "duplicate email" in low
                        or "email already" in low
                        # DB-side unique violation on the internal email /
                        # username columns (asyncpg: 'duplicate key value
                        # violates unique constraint "uq_xui_clients_email"' or
                        # "…_username"). Both are fixed by a suffixed retry.
                        # NOTE: deliberately NOT matching the subscription_id
                        # constraint — that one is handled by reusing the
                        # existing row above, and a suffix wouldn't fix it.
                        or "uq_xui_clients_email" in low
                        or "uq_xui_clients_username" in low
                    )
                    if is_duplicate and attempt_idx < len(email_candidates) - 1:
                        last_dup_exc = inner_exc
                        logger.warning(
                            "Migration email/username %r collided (panel or DB), retrying with suffix…",
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

        # ── Best-effort: delete the OLD client from the panel ──────────
        # The imported config's client (consolidated onto this panel from
        # the legacy bot) is still sitting on its original inbound under
        # the same email/uuid. Now that we've created the fresh migrated
        # client, remove the stale one so the user has exactly ONE client
        # per config. Never touches the client we just created (keep_uuid).
        try:
            await self._delete_stale_panel_clients(
                server=target_server,
                remark=remark,
                old_uuid=_old_uuid,
                keep_uuid=client_uuid,
            )
        except Exception as exc:
            logger.warning(
                "imported-migration: stale-client cleanup failed (non-fatal) for sub %s: %s",
                sub.id, exc,
            )

        return MigrationResult(
            subscription=sub,
            xui_client=xui,
            new_vless_uri=new_vless_uri,
            new_sub_link=new_sub_link,
            old_inbound_label="(ربات قبلی)",
            new_inbound_label=new_inbound_label,
            remaining_bytes=remaining_bytes,
        )

    async def _delete_stale_panel_clients(
        self,
        *,
        server: XUIServerRecord,
        remark: str | None,
        old_uuid: str | None,
        keep_uuid: str,
    ) -> int:
        """Find and delete panel clients that match this config's OLD
        identity (email == remark, or client id == old_uuid) across ALL
        inbounds on `server`, EXCLUDING the freshly-created client
        (keep_uuid). Returns how many were deleted.

        Bounded by a 25s asyncio timeout; best-effort. One get_inbounds +
        N delClient calls. (A single migration, so the big-panel parse
        cost is paid once — not per-sub.)
        """
        import asyncio as _asyncio
        import json as _json

        if not remark and not old_uuid:
            return 0
        remark_l = (remark or "").strip().lower()

        async def _run() -> int:
            async with self._get_xui_client_for_server(server) as api:
                inbounds = await api.get_inbounds()
                # Collect (inbound_remote_id, client_id) to delete.
                to_delete: list[tuple[int, str]] = []
                for ib in inbounds:
                    settings = ib.settings or {}
                    if isinstance(settings, str):
                        try:
                            settings = _json.loads(settings)
                        except Exception:
                            settings = {}
                    for c in (settings.get("clients") or []):
                        cid = str(c.get("id") or c.get("uuid") or "").strip()
                        cemail = str(c.get("email") or "").strip().lower()
                        if cid and cid == keep_uuid:
                            continue  # never delete the new client
                        matches = (
                            (remark_l and cemail == remark_l)
                            or (old_uuid and cid == old_uuid)
                        )
                        if matches and cid:
                            to_delete.append((ib.xui_inbound_remote_id, cid))

                # COLLATERAL-DELETION GUARD: the `email == legacy_remark`
                # heuristic can match a DIFFERENT user's live config if their
                # panel email happens to equal this sub's legacy remark. Never
                # delete a client that one of OUR XUIClientRecords manages — the
                # stale legacy client we actually want to remove has no record
                # on our side (imported subs are untracked until migrated).
                if to_delete:
                    candidate_cids = [cid for _, cid in to_delete]
                    managed_rows = await self.session.execute(
                        select(XUIClientRecord.client_uuid, XUIClientRecord.xui_client_remote_id)
                        .where(
                            (XUIClientRecord.client_uuid.in_(candidate_cids))
                            | (XUIClientRecord.xui_client_remote_id.in_(candidate_cids))
                        )
                    )
                    managed_cids: set[str] = set()
                    for cu, crid in managed_rows.all():
                        if cu:
                            managed_cids.add(str(cu))
                        if crid:
                            managed_cids.add(str(crid))
                    if managed_cids:
                        skipped = [d for d in to_delete if d[1] in managed_cids]
                        if skipped:
                            logger.warning(
                                "imported-migration: skipping %d stale-client candidate(s) that "
                                "are managed by an existing record (collateral-deletion guard): %s",
                                len(skipped), [cid for _, cid in skipped],
                            )
                        to_delete = [d for d in to_delete if d[1] not in managed_cids]

                deleted = 0
                for inbound_remote_id, cid in to_delete:
                    try:
                        await api.delete_client(inbound_id=inbound_remote_id, client_id=cid)
                        deleted += 1
                        logger.info(
                            "imported-migration: deleted stale client %s from inbound %s",
                            cid, inbound_remote_id,
                        )
                    except Exception as del_exc:
                        logger.warning(
                            "imported-migration: failed to delete stale client %s: %s",
                            cid, del_exc,
                        )
                return deleted

        try:
            return await _asyncio.wait_for(_run(), timeout=25.0)
        except _asyncio.TimeoutError:
            logger.warning("imported-migration: stale-client cleanup timed out (25s)")
            return 0

    async def _read_legacy_client_usage(self, server: XUIServerRecord, remark: str | None) -> int:
        """Best-effort: how many bytes the OLD legacy/consolidated client
        (email == remark) already consumed. Used at migration time to provision
        only the REMAINING quota. Returns 0 if not found / on any error so the
        migration never fails because of this. Bounded by a 12s timeout.

        Reliable at migration time because the NEW client doesn't exist yet, so
        getClientTraffics(remark) resolves only the old client.
        """
        if not remark:
            return 0
        import asyncio as _asyncio

        async def _run() -> int:
            async with self._get_xui_client_for_server(server) as api:
                traffic = await api.get_client_traffic(remark.strip())
                return max(int(traffic.used_bytes or 0), 0)

        try:
            return await _asyncio.wait_for(_run(), timeout=12.0)
        except Exception as exc:
            logger.warning("[MIGRATE-USAGE] could not read legacy usage for remark=%r: %s", remark, exc)
            return 0

    async def reconcile_migrated_usage_for_server(
        self,
        server: XUIServerRecord,
        *,
        limit: int = 500,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Backfill fix for configs migrated BEFORE the usage-subtraction fix.

        Those were provisioned with their FULL purchased volume even though the
        user had already consumed part of it on the old client. We find each
        migrated config's OLD client on the panel (one get_inbounds, matched by
        email == legacy_remark, case-insensitive, on a DIFFERENT inbound than
        the new client) and set the NEW client's quota to the OLD client's REAL
        remaining = old.totalGB - old.used.

        IDEMPOTENT: the result is computed purely from the OLD client's frozen
        panel counters (an absolute value), NOT from the current DB volume — so
        re-running converges to the same correct value and can NEVER double-
        subtract. Safe to run repeatedly / with force.

        `force`   — ignore the reconciled marker and re-check every migrated sub.
        `dry_run` — read + report only, change nothing (no panel write, no DB).

        Returns a dict with counts + a `details` list (per-sub diagnostics) +
        a `panel_emails` sample (to debug name mismatches).
        """
        import asyncio as _asyncio
        from collections import defaultdict
        from sqlalchemy import update as _sa_update

        out: dict = {"checked": 0, "fixed": 0, "no_data": 0, "skipped": 0, "details": [], "panel_emails": []}

        # PasarGuard has no X-UI-style legacy-migration backlog (no client_stats
        # shape to read), so there is nothing to reconcile here.
        if is_pasarguard(server):
            return out

        # Re-fetch the server WITH credentials eager-loaded. build_xui_client_config
        # reads server.credentials; if the caller passed a server without that
        # relationship loaded, accessing it inside the async panel context would
        # raise MissingGreenlet (a lazy load mid-await). Guarantee it here.
        server = await self.session.scalar(
            select(XUIServerRecord)
            .options(selectinload(XUIServerRecord.credentials))
            .where(XUIServerRecord.id == server.id)
        )
        if server is None or server.credentials is None:
            return out

        cond = [
            Subscription.source.is_(None),
            Subscription.legacy_remark.isnot(None),
            XUIInboundRecord.server_id == server.id,
        ]
        if not force:
            cond.append(Subscription.migration_usage_reconciled.is_(False))

        # IMPORTANT: select COLUMNS, not ORM objects. Loading Subscription rows
        # with relationships and then reading xc.inbound / sub.plan inside the
        # async panel block is what raised MissingGreenlet (a lazy load mid
        # await). Row tuples carry no relationships, so nothing can lazy-load.
        rows = await self.session.execute(
            select(
                Subscription.id.label("sub_id"),
                Subscription.legacy_remark,
                Subscription.lifetime_used_bytes,
                Subscription.ends_at,
                Subscription.sub_link,
                XUIClientRecord.client_uuid,
                XUIClientRecord.xui_client_remote_id,
                XUIClientRecord.email,
                XUIClientRecord.username,
                XUIClientRecord.is_active,
                XUIClientRecord.sub_link.label("xc_sub_link"),
                XUIInboundRecord.xui_inbound_remote_id.label("new_inbound"),
                Plan.ip_limit.label("plan_ip_limit"),
            )
            .join(XUIClientRecord, XUIClientRecord.subscription_id == Subscription.id)
            .join(XUIInboundRecord, XUIInboundRecord.id == XUIClientRecord.inbound_id)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
            .where(*cond)
            .limit(limit)
        )
        subs = rows.all()
        if not subs:
            return out

        global_ip_limit = (await AppSettingsRepository(self.session).get_service_security_settings()).xui_limit_ip

        # ONE get_inbounds → map normalized-email -> [{inbound, total, used, email}].
        clients_by_email: dict[str, list[dict]] = defaultdict(list)

        async def _load_map(api) -> None:
            inbounds = await api.get_inbounds()
            for ib in inbounds:
                for cs in (ib.client_stats or []):
                    if not isinstance(cs, dict):
                        continue
                    email_raw = str(cs.get("email") or "").strip()
                    if not email_raw:
                        continue
                    clients_by_email[email_raw.lower()].append({
                        "inbound": int(ib.id),
                        "total": int(cs.get("total") or 0),
                        "used": int(cs.get("up") or 0) + int(cs.get("down") or 0),
                        "email": email_raw,
                    })

        async with self._get_xui_client_for_server(server) as api:
            await _asyncio.wait_for(_load_map(api), timeout=90.0)
            out["panel_emails"] = sorted(clients_by_email.keys())[:20]

            for r in subs:
                out["checked"] += 1
                new_inbound = r.new_inbound
                remark_norm = (r.legacy_remark or "").strip().lower()
                # Old client = same email on a DIFFERENT inbound than the new
                # one (never the migrated client). If several, take the one with
                # the largest quota (the real original config).
                candidates = [
                    c for c in clients_by_email.get(remark_norm, [])
                    if c["inbound"] != new_inbound
                ]
                old = max(candidates, key=lambda c: c["total"], default=None)
                detail = {"remark": r.legacy_remark, "new_email": r.email}
                if old is None or old["total"] <= 0:
                    detail["result"] = "no_old_client" if old is None else "old_unlimited"
                    out["no_data"] += 1
                    out["details"].append(detail)
                    continue

                new_total = old["total"] - old["used"]
                if new_total <= 0:
                    new_total = 1  # depleted; never 0 == unlimited
                detail.update({"old_total": old["total"], "old_used": old["used"], "new_total": new_total})
                if dry_run:
                    detail["result"] = "would_fix"
                    out["fixed"] += 1
                    out["details"].append(detail)
                    continue
                try:
                    async with self.session.begin_nested():
                        await self.session.execute(
                            _sa_update(Subscription)
                            .where(Subscription.id == r.sub_id)
                            .values(
                                volume_bytes=new_total,
                                lifetime_used_bytes=max(int(r.lifetime_used_bytes or 0), old["used"]),
                                migration_usage_reconciled=True,
                            )
                        )
                        client_id = r.xui_client_remote_id or r.client_uuid
                        sub_id_str = ""
                        link = r.sub_link or r.xc_sub_link or ""
                        if "/" in link:
                            sub_id_str = link.rsplit("/", 1)[-1]
                        ip_limit = int(r.plan_ip_limit) if r.plan_ip_limit is not None else global_ip_limit
                        expiry_ms = int(r.ends_at.timestamp() * 1000) if r.ends_at else 0
                        await api.update_client(
                            inbound_id=new_inbound,
                            client_id=client_id,
                            client=XUIClient(
                                id=client_id, uuid=r.client_uuid, email=r.email,
                                limitIp=ip_limit, totalGB=new_total, expiryTime=expiry_ms,
                                enable=r.is_active, subId=sub_id_str, comment=r.username or "",
                            ),
                        )
                    detail["result"] = "fixed"
                    out["fixed"] += 1
                    logger.info(
                        "[MIGRATE-USAGE] fixed sub=%s: old_total=%d old_used=%d → new=%d",
                        r.sub_id, old["total"], old["used"], new_total,
                    )
                except Exception as exc:
                    detail["result"] = f"error: {str(exc)[:80]}"
                    logger.warning("[MIGRATE-USAGE] failed to reconcile sub %s: %s", r.sub_id, exc)
                out["details"].append(detail)

        return out

    async def _recover_from_sublink_header(self, sub: "Subscription") -> tuple[int | None, int | None]:
        """Best-effort: read total + expire from the `subscription-userinfo`
        HTTP header on the sub's subscription link.

        Returns (total_bytes, expire_ms) — either may be None. Fully
        bounded by a 12s asyncio timeout; never raises. Picks the first
        candidate that is an HTTP(S) URL (a vless:// URI can't be fetched
        and is skipped instantly).
        """
        import asyncio as _asyncio
        import httpx as _httpx

        link: str | None = None
        for candidate in (sub.sub_link, sub.legacy_link):
            if not candidate:
                continue
            c = candidate.strip()
            if c.startswith(("http://", "https://")):
                link = c
                break
            logger.info("[SUBINFO] skipping non-HTTP candidate: %s", c[:80])
        if not link:
            logger.info("[SUBINFO] no HTTP sub-link to read — relying on DB volume / re-import")
            return None, None

        async def _do() -> tuple[int | None, int | None]:
            logger.info("[SUBINFO] fetching sub-link: %s", link)
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(10.0, connect=5.0),
                follow_redirects=True,
                verify=False,  # legacy panels frequently use self-signed certs
            ) as client:
                resp = await client.get(link, headers={"User-Agent": "v2rayN/6.0"})
            userinfo = None
            for hk, hv in resp.headers.items():
                if hk.lower() == "subscription-userinfo":
                    userinfo = hv
                    break
            if not userinfo:
                logger.info("[SUBINFO] no subscription-userinfo header present")
                return None, None
            logger.info("[SUBINFO] subscription-userinfo: %s", userinfo)
            parsed: dict[str, int] = {}
            for chunk in userinfo.split(";"):
                if "=" not in chunk:
                    continue
                k, v = chunk.strip().split("=", 1)
                try:
                    parsed[k.strip().lower()] = int(v.strip())
                except ValueError:
                    continue
            total = parsed.get("total")
            expire_s = parsed.get("expire")
            expire_ms = expire_s * 1000 if expire_s and expire_s > 0 else None
            return total, expire_ms

        try:
            return await _asyncio.wait_for(_do(), timeout=12.0)
        except _asyncio.TimeoutError:
            logger.warning("[SUBINFO] sub-link read timed out (12s)")
            return None, None
        except Exception as exc:
            logger.warning("[SUBINFO] sub-link read failed: %s: %s", type(exc).__name__, exc)
            return None, None

    async def disable_user_active_configs(self, user_id: UUID) -> int:
        """Disable all of a user's active/pending configs on the X-UI panel.

        Used by the ban flows so a banned user's VPN actually stops working,
        not just their bot access. Sets each sub to `disabled` and best-effort
        disables the remote client (failures are logged, not fatal). Returns
        how many subs were affected. Shared by the bot, dashboard, and mini-app
        ban paths so all three behave identically.
        """
        result = await self.session.execute(
            select(Subscription)
            .options(
                selectinload(Subscription.xui_client)
                .selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                # Load credentials too: both the X-UI and PasarGuard disable
                # paths need them, and without eager-loading they would lazy-load
                # (forbidden in async → silent best-effort failure, leaving a
                # banned user's config still live on the panel).
                .selectinload(XUIServerRecord.credentials)
            )
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(["active", "pending_activation"]),
            )
        )
        disabled_count = 0
        for subscription in result.scalars().all():
            subscription.status = "disabled"
            xui_record = subscription.xui_client
            if xui_record is not None:
                xui_record.is_active = False
                try:
                    await self._disable_xui_client(
                        xui_record=xui_record,
                        volume_bytes=subscription.volume_bytes,
                        ends_at=subscription.ends_at,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not disable remote config %s for banned user %s: %s",
                        xui_record.id, user_id, exc,
                    )
            disabled_count += 1
        return disabled_count

    async def _disable_pasarguard_client(self, xui_record: XUIClientRecord) -> None:
        """Disable a PasarGuard config (PUT status=disabled). PG keeps the sub
        link valid but blocks traffic — same UX intent as the X-UI disable."""
        inbound = xui_record.inbound
        if inbound is None:
            raise ProvisioningError("Panel inbound mapping is missing.")
        server = ensure_inbound_server_loaded(inbound)
        username = xui_record.panel_username or xui_record.username
        async with create_pasarguard_client_for_server(server) as client:
            await client.modify_user(username, PGUserModify(status="disabled"))

    async def _disable_xui_client(
        self,
        *,
        xui_record: XUIClientRecord,
        volume_bytes: int,
        ends_at: datetime | None,
    ) -> None:
        # PasarGuard configs disable via their own (user-centric) API.
        if record_is_pasarguard(xui_record):
            await self._disable_pasarguard_client(xui_record)
            return

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
