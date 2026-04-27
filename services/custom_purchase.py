from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.plan import Plan
from repositories.settings import CustomPurchaseSettings


CUSTOM_PLAN_CODE_PREFIX = "custom_"


class CustomPurchaseError(ValueError):
    pass


def is_custom_purchase_plan(plan: Plan | None) -> bool:
    return bool(plan and plan.code and plan.code.startswith(CUSTOM_PLAN_CODE_PREFIX))


def calculate_custom_purchase_price(
    settings: CustomPurchaseSettings,
    *,
    volume_gb: float,
    duration_days: int,
) -> Decimal:
    if not settings.enabled:
        raise CustomPurchaseError("خرید دلخواه غیرفعال است.")
    if settings.price_per_gb <= 0 or settings.price_per_day <= 0:
        raise CustomPurchaseError("قیمت خرید دلخواه تنظیم نشده است.")
    if volume_gb <= 0:
        raise CustomPurchaseError("حجم باید بیشتر از صفر باشد.")
    if duration_days <= 0:
        raise CustomPurchaseError("مدت باید بیشتر از صفر باشد.")

    price = (
        Decimal(str(volume_gb)) * Decimal(str(settings.price_per_gb))
        + Decimal(str(duration_days)) * Decimal(str(settings.price_per_day))
    )
    return price.quantize(Decimal("0.01"))


async def get_custom_purchase_template_plan(session: AsyncSession) -> Plan | None:
    return await session.scalar(
        select(Plan)
        .where(
            Plan.is_active.is_(True),
            Plan.inbound_id.isnot(None),
            not_(Plan.code.like("custom\\_%", escape="\\")),
            not_(Plan.code.like("ready\\_%", escape="\\")),
            ~Plan.name.startswith("[حذف شده]"),
        )
        .order_by(Plan.created_at.asc())
        .limit(1)
    )


async def create_custom_purchase_plan(
    session: AsyncSession,
    *,
    volume_gb: float,
    duration_days: int,
    price: Decimal,
    template_plan: Plan,
) -> Plan:
    if template_plan.inbound_id is None:
        raise CustomPurchaseError("پلن پایه خرید دلخواه اینباند ندارد.")

    volume_bytes = int(volume_gb * 1024**3)
    if volume_bytes <= 0:
        raise CustomPurchaseError("حجم باید بیشتر از صفر باشد.")

    clean_volume = format(Decimal(str(volume_gb)).normalize(), "f").rstrip("0").rstrip(".")
    code = f"{CUSTOM_PLAN_CODE_PREFIX}{duration_days}d_{clean_volume}gb_{uuid4().hex[:10]}"
    plan = Plan(
        code=code,
        name=f"دلخواه {clean_volume}GB / {duration_days} روز",
        protocol=template_plan.protocol,
        inbound_id=template_plan.inbound_id,
        duration_days=duration_days,
        volume_bytes=volume_bytes,
        price=price,
        renewal_price=price,
        currency=template_plan.currency or "USD",
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    return plan
