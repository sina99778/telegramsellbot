from __future__ import annotations

import logging
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
from schemas.internal.xui import XUIClient
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


@dataclass(slots=True, frozen=True)
class ProvisioningResult:
    subscription: Subscription
    xui_client: XUIClientRecord | None
    vless_uri: str
    sub_link: str


class ProvisioningManager:
    def __init__(self, session: AsyncSession, xui_client: SanaeiXUIClient | None = None) -> None:
        self.session = session
        self.xui_client = xui_client
        self.wallet_manager = WalletManager(session)

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

        ready_pool = await self.session.scalar(
            select(ReadyConfigPool).where(
                ReadyConfigPool.plan_id == plan.id,
                ReadyConfigPool.is_active.is_(True),
            )
        )
        if ready_pool is not None:
            return await self._provision_ready_config(
                user_id=user_id,
                plan=plan,
                order=order,
                pool=ready_pool,
            )

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
        sub_link = build_sub_link(server, sub_id)
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
            limitIp=1,
            totalGB=plan.volume_bytes,
            expiryTime=0,
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

        async with self._get_xui_client_for_server(server) as xui_client:
            await xui_client.add_client_to_inbound(inbound.xui_inbound_remote_id, xui_payload)

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
        await self.session.refresh(subscription)
        await self.session.refresh(xui_record)
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
            status="active",
            activation_mode="ready_config",
            starts_at=now,
            ends_at=now + timedelta(days=plan.duration_days),
            activated_at=now,
            expired_at=None,
            volume_bytes=plan.volume_bytes,
            used_bytes=0,
            sub_link=item.content,
        )
        self.session.add(subscription)
        await self.session.flush()

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
            vless_uri=item.content,
            sub_link=item.content,
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
        )
        if subscription is None:
            raise ZeroUsageRefundError("Subscription not found.")
        if subscription.status != "pending_activation":
            raise ZeroUsageRefundError("Only pending_activation subscriptions can be refunded.")
        if subscription.used_bytes != 0:
            raise ZeroUsageRefundError("Subscription has usage and is not eligible for zero-usage refund.")
        if subscription.order is None or subscription.plan is None:
            raise ZeroUsageRefundError("Subscription is missing order or plan references.")

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
        disabled_client = XUIClient(
            id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            uuid=xui_record.client_uuid,
            email=xui_record.email,
            limitIp=1,
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
