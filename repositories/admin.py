from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.order import Order
from models.subscription import Subscription
from models.user import User
from models.xui import XUIServerRecord


class AdminStatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_total_users(self) -> int:
        result = await self.session.scalar(select(func.count()).select_from(User))
        return int(result or 0)

    async def get_total_active_subscriptions(self) -> int:
        result = await self.session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.status == "active")
        )
        return int(result or 0)

    async def get_total_revenue(self, reset_at: datetime | None = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(Order.amount), 0)).select_from(Order).where(
            Order.status.in_(["paid", "processing", "provisioned"])
        )
        if reset_at:
            stmt = stmt.where(Order.created_at >= reset_at)
            
        result = await self.session.scalar(stmt)
        return Decimal(str(result or 0))

    async def get_total_active_servers(self) -> int:
        result = await self.session.scalar(
            select(func.count()).select_from(XUIServerRecord).where(XUIServerRecord.is_active.is_(True))
        )
        return int(result or 0)

    # Statuses whose delivered bytes should be counted toward reseller
    # billing. We INCLUDE expired (those bytes were actually consumed),
    # active and pending_activation. We EXCLUDE refunded (operator gave
    # the money back) and disabled (admin-suspended; usually compensated
    # separately). cancelled is excluded too: typically a customer
    # cancellation happens before significant usage.
    _BILLABLE_STATUSES: tuple[str, ...] = ("active", "pending_activation", "expired")

    async def get_total_used_bytes(self) -> int:
        """Total bytes delivered to customers.

        IMPORTANT: every code path that resets ``Subscription.used_bytes``
        to 0 (volume renewal at services/renewal.py and inbound migration
        at services/provisioning/manager.py) FIRST accumulates the
        previous-cycle bytes into ``lifetime_used_bytes``. So the
        right "total delivered" sum is the column pair, not just
        ``used_bytes`` — that one only holds the *current* cycle.

        Resellers bill the operator on this number. If you ever change
        the sum, double-check that renewals and migrations still
        accumulate into ``lifetime_used_bytes`` first, or you'll silently
        under-bill yourself.
        """
        result = await self.session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(Subscription.lifetime_used_bytes, 0)
                        + func.coalesce(Subscription.used_bytes, 0)
                    ),
                    0,
                )
            ).where(Subscription.status.in_(self._BILLABLE_STATUSES))
        )
        return int(result or 0)
