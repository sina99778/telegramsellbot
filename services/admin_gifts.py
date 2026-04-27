from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord
from services.renewal import apply_renewal


ACTIVE_GIFT_STATUSES = ("active", "pending_activation")
ALL_GIFT_STATUSES = ("active", "pending_activation", "expired")


@dataclass(slots=True)
class BulkGiftResult:
    matched_count: int = 0
    updated_count: int = 0
    failed_count: int = 0
    failed_ids: list[str] | None = None


def get_gift_statuses(status_scope: str) -> tuple[str, ...]:
    if status_scope == "active":
        return ACTIVE_GIFT_STATUSES
    if status_scope == "all":
        return ALL_GIFT_STATUSES
    raise ValueError("Invalid gift status scope.")


async def grant_bulk_subscription_gift(
    *,
    session: AsyncSession,
    gift_type: str,
    amount: float,
    status_scope: str,
    server_id: UUID | None = None,
) -> BulkGiftResult:
    if gift_type not in {"time", "volume"}:
        raise ValueError("Invalid gift type.")
    if amount <= 0:
        raise ValueError("Gift amount must be positive.")

    statuses = get_gift_statuses(status_scope)
    stmt = (
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server),
            selectinload(Subscription.plan),
        )
        .where(Subscription.status.in_(statuses))
    )
    if server_id is not None:
        stmt = (
            stmt.join(XUIClientRecord, XUIClientRecord.subscription_id == Subscription.id)
            .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
            .where(XUIInboundRecord.server_id == server_id)
        )

    result = await session.execute(stmt)
    subscriptions = list(result.scalars().unique().all())
    gift_result = BulkGiftResult(matched_count=len(subscriptions), failed_ids=[])

    for subscription in subscriptions:
        try:
            await apply_renewal(
                session=session,
                subscription=subscription,
                renew_type=gift_type,
                amount=amount,
            )
            gift_result.updated_count += 1
        except Exception:
            gift_result.failed_count += 1
            gift_result.failed_ids.append(str(subscription.id))

    await session.flush()
    return gift_result
