from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from models.order import Order
from models.plan import Plan
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord
from schemas.internal.xui import XUIClient
from services.wallet.manager import WalletManager
from services.xui.client import SanaeiXUIClient


class ProvisioningError(Exception):
    """Base provisioning domain error."""


class ProvisioningConflictError(ProvisioningError):
    """Raised when a unique X-UI identity cannot be generated safely."""


class ZeroUsageRefundError(ProvisioningError):
    """Raised when a zero-usage refund request is invalid."""


@dataclass(slots=True, frozen=True)
class ProvisioningResult:
    subscription: Subscription
    xui_client: XUIClientRecord


class ProvisioningManager:
    def __init__(self, session: AsyncSession, xui_client: SanaeiXUIClient) -> None:
        self.session = session
        self.xui_client = xui_client
        self.wallet_manager = WalletManager(session)

    async def provision_subscription(
        self,
        *,
        user_id: UUID,
        plan_id: UUID,
        order_id: UUID,
    ) -> ProvisioningResult:
        plan = await self.session.get(Plan, plan_id)
        if plan is None or not plan.is_active:
            raise ProvisioningError("Plan not found or inactive.")

        order = await self.session.get(Order, order_id)
        if order is None or order.user_id != user_id:
            raise ProvisioningError("Order not found for user.")

        inbound = await self.session.scalar(
            select(XUIInboundRecord)
            .where(XUIInboundRecord.is_active.is_(True))
            .order_by(XUIInboundRecord.created_at.asc())
            .limit(1)
        )
        if inbound is None:
            raise ProvisioningError("No active X-UI inbound is available for provisioning.")

        client_uuid, username, email, sub_id = await self._generate_unique_client_identity()
        sub_link = self._build_sub_link(sub_id)
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

        await self.xui_client.add_client_to_inbound(inbound.xui_inbound_remote_id, xui_payload)

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
            username=username,
            sub_link=sub_link,
            usage_bytes=0,
            is_active=True,
        )
        self.session.add(xui_record)

        order.status = "provisioned"

        await self.session.flush()
        await self.session.refresh(subscription)
        await self.session.refresh(xui_record)
        return ProvisioningResult(subscription=subscription, xui_client=xui_record)

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
                selectinload(Subscription.xui_client).selectinload(XUIClientRecord.inbound),
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

    @staticmethod
    def _build_sub_link(sub_id: str) -> str:
        return f"{settings.xui_base_url.rstrip('/')}/sub/{sub_id}"

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
        await self.xui_client.update_client(
            inbound_id=inbound.xui_inbound_remote_id,
            client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
            client=disabled_client,
        )
