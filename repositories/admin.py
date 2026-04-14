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
