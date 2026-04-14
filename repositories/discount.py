from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discount import DiscountCode


class DiscountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, code: str) -> DiscountCode | None:
        result = await self.session.execute(
            select(DiscountCode).where(DiscountCode.code == code.strip().upper())
        )
        return result.scalar_one_or_none()

    async def validate_code(self, code: str, plan_id: UUID | None = None) -> DiscountCode | None:
        """Return DiscountCode if valid, None otherwise."""
        discount = await self.get_by_code(code)
        if discount is None:
            return None
        if not discount.is_active:
            return None
        if discount.used_count >= discount.max_uses:
            return None
        if discount.expires_at and discount.expires_at < datetime.now(timezone.utc):
            return None
        if discount.plan_id and plan_id and discount.plan_id != plan_id:
            return None
        return discount

    async def use_code(self, discount: DiscountCode) -> None:
        discount.used_count += 1
        if discount.used_count >= discount.max_uses:
            discount.is_active = False
        self.session.add(discount)
        await self.session.flush()

    async def create_code(
        self,
        *,
        code: str,
        discount_percent: int,
        max_uses: int = 1,
        expires_at: datetime | None = None,
        plan_id: UUID | None = None,
    ) -> DiscountCode:
        dc = DiscountCode(
            code=code.strip().upper(),
            discount_percent=discount_percent,
            max_uses=max_uses,
            expires_at=expires_at,
            plan_id=plan_id,
        )
        self.session.add(dc)
        await self.session.flush()
        await self.session.refresh(dc)
        return dc

    async def list_active(self, limit: int = 20) -> list[DiscountCode]:
        result = await self.session.execute(
            select(DiscountCode)
            .where(DiscountCode.is_active.is_(True))
            .order_by(DiscountCode.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def deactivate(self, discount: DiscountCode) -> None:
        discount.is_active = False
        self.session.add(discount)
        await self.session.flush()
