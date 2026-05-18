from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discount import DiscountCode


class DiscountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, discount_id: UUID) -> DiscountCode | None:
        return await self.session.get(DiscountCode, discount_id)

    async def get_by_code(self, code: str) -> DiscountCode | None:
        result = await self.session.execute(
            select(DiscountCode).where(DiscountCode.code == code.strip().upper())
        )
        return result.scalar_one_or_none()

    async def validate_code(self, code: str, plan_id: UUID | None = None) -> DiscountCode | None:
        """Return DiscountCode if valid, None otherwise."""
        discount, _reason = await self.validate_code_with_reason(code, plan_id=plan_id)
        return discount

    async def validate_code_with_reason(
        self,
        code: str,
        plan_id: UUID | None = None,
    ) -> tuple[DiscountCode | None, str | None]:
        """Like validate_code but also returns a user-friendly Persian reason
        when the code is not usable. Reasons:
          - 'not_found'  → کد وارد شده وجود ندارد
          - 'inactive'   → کد غیرفعال شده است
          - 'exhausted'  → سقف استفاده این کد پر شده
          - 'expired'    → کد تخفیف منقضی شده
          - 'plan_mismatch' → کد برای پلن دیگری است
        """
        discount = await self.get_by_code(code)
        if discount is None:
            return None, "not_found"
        if not discount.is_active:
            return None, "inactive"
        if discount.used_count >= discount.max_uses:
            return None, "exhausted"
        if discount.expires_at and discount.expires_at < datetime.now(timezone.utc):
            return None, "expired"
        if discount.plan_id and plan_id and discount.plan_id != plan_id:
            return None, "plan_mismatch"
        return discount, None

    async def use_code(
        self,
        discount: DiscountCode,
        *,
        plan_id: UUID | None = None,
    ) -> DiscountCode | None:
        """Atomically consume a usage slot on the discount.

        Re-validates every constraint inside the row lock so a code that
        expired, was deactivated, or got exhausted between the original
        validate_code() call and now cannot be silently used. Returns the
        locked DiscountCode on success or None if it is no longer usable —
        callers MUST treat None as "discount cannot be applied" and recompute
        the price without the discount.
        """
        locked = await self.session.scalar(
            select(DiscountCode)
            .where(DiscountCode.id == discount.id)
            .with_for_update()
        )
        if locked is None:
            return None
        if not locked.is_active:
            return None
        if locked.used_count >= locked.max_uses:
            return None
        if locked.expires_at and locked.expires_at < datetime.now(timezone.utc):
            return None
        if locked.plan_id and plan_id and locked.plan_id != plan_id:
            return None
        locked.used_count += 1
        if locked.used_count >= locked.max_uses:
            locked.is_active = False
        self.session.add(locked)
        await self.session.flush()
        return locked

    async def create_code(
        self,
        *,
        code: str,
        discount_percent: int,
        max_uses: int = 1,
        expires_at: datetime | None = None,
        plan_id: UUID | None = None,
    ) -> DiscountCode:
        if not 0 <= discount_percent <= 100:
            raise ValueError("discount_percent must be between 0 and 100")
        if max_uses < 1:
            raise ValueError("max_uses must be >= 1")
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
