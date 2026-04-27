from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.plan_inventory import PlanInventory
from models.ready_config import ReadyConfigItem, ReadyConfigPool
from models.subscription import Subscription


UNLIMITED_STOCK_LIMIT = 0
COUNTED_SUBSCRIPTION_STATUSES = ("active", "pending_activation", "expired")


class PlanStockError(Exception):
    """Raised when a limited plan has no stock left."""


@dataclass(slots=True, frozen=True)
class PlanStockState:
    sales_limit: int
    sold_count: int
    stock_remaining: int | None
    is_unlimited: bool


def build_stock_state(inventory: PlanInventory | None) -> PlanStockState:
    if inventory is None or inventory.sales_limit <= UNLIMITED_STOCK_LIMIT:
        return PlanStockState(
            sales_limit=UNLIMITED_STOCK_LIMIT,
            sold_count=inventory.sold_count if inventory else 0,
            stock_remaining=None,
            is_unlimited=True,
        )
    remaining = max(inventory.sales_limit - inventory.sold_count, 0)
    return PlanStockState(
        sales_limit=inventory.sales_limit,
        sold_count=inventory.sold_count,
        stock_remaining=remaining,
        is_unlimited=False,
    )


async def get_plan_stock_map(
    session: AsyncSession,
    plan_ids: list[UUID],
) -> dict[UUID, PlanStockState]:
    if not plan_ids:
        return {}
    result = await session.execute(
        select(PlanInventory).where(PlanInventory.plan_id.in_(plan_ids))
    )
    inventories = {inventory.plan_id: inventory for inventory in result.scalars().all()}
    return {plan_id: build_stock_state(inventories.get(plan_id)) for plan_id in plan_ids}


async def get_effective_plan_stock_map(
    session: AsyncSession,
    plan_ids: list[UUID],
) -> dict[UUID, PlanStockState]:
    stock_by_plan_id = await get_plan_stock_map(session, plan_ids)
    ready_counts = await _get_ready_config_available_counts(session, plan_ids)
    for plan_id, ready_available in ready_counts.items():
        stock_by_plan_id[plan_id] = _merge_ready_config_stock(stock_by_plan_id[plan_id], ready_available)
    return stock_by_plan_id


def is_stock_available(stock: PlanStockState) -> bool:
    return stock.is_unlimited or (stock.stock_remaining or 0) > 0


async def ensure_plan_available(session: AsyncSession, plan_id: UUID) -> None:
    stock_map = await get_effective_plan_stock_map(session, [plan_id])
    if not is_stock_available(stock_map[plan_id]):
        raise PlanStockError("Plan stock is sold out.")


async def reserve_plan_sale(session: AsyncSession, plan_id: UUID) -> bool:
    inventory = await session.scalar(
        select(PlanInventory)
        .where(PlanInventory.plan_id == plan_id)
        .with_for_update()
    )
    if inventory is None or inventory.sales_limit <= UNLIMITED_STOCK_LIMIT:
        return False
    if inventory.sold_count >= inventory.sales_limit:
        raise PlanStockError("Plan stock is sold out.")
    inventory.sold_count += 1
    await session.flush()
    return True


async def release_plan_sale(session: AsyncSession, plan_id: UUID) -> None:
    inventory = await session.scalar(
        select(PlanInventory)
        .where(PlanInventory.plan_id == plan_id)
        .with_for_update()
    )
    if inventory is None or inventory.sales_limit <= UNLIMITED_STOCK_LIMIT:
        return
    inventory.sold_count = max(inventory.sold_count - 1, 0)
    await session.flush()


async def set_plan_sales_limit(
    session: AsyncSession,
    plan_id: UUID,
    sales_limit: int,
) -> PlanStockState:
    normalized_limit = max(int(sales_limit), UNLIMITED_STOCK_LIMIT)
    inventory = await session.scalar(
        select(PlanInventory)
        .where(PlanInventory.plan_id == plan_id)
        .with_for_update()
    )
    if inventory is None:
        sold_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.plan_id == plan_id,
                    Subscription.status.in_(COUNTED_SUBSCRIPTION_STATUSES),
                )
            )
            or 0
        )
        inventory = PlanInventory(
            plan_id=plan_id,
            sales_limit=normalized_limit,
            sold_count=sold_count,
        )
        session.add(inventory)
    else:
        inventory.sales_limit = normalized_limit
    await session.flush()
    return build_stock_state(inventory)


async def _get_ready_config_available_counts(
    session: AsyncSession,
    plan_ids: list[UUID],
) -> dict[UUID, int]:
    if not plan_ids:
        return {}
    result = await session.execute(
        select(ReadyConfigPool.plan_id, func.count(ReadyConfigItem.id))
        .outerjoin(
            ReadyConfigItem,
            and_(
                ReadyConfigItem.pool_id == ReadyConfigPool.id,
                ReadyConfigItem.status == "available",
            ),
        )
        .where(
            ReadyConfigPool.plan_id.in_(plan_ids),
            ReadyConfigPool.is_active.is_(True),
        )
        .group_by(ReadyConfigPool.plan_id)
    )
    return {plan_id: int(available_count or 0) for plan_id, available_count in result.all()}


def _merge_ready_config_stock(stock: PlanStockState, ready_available: int) -> PlanStockState:
    if stock.is_unlimited:
        return PlanStockState(
            sales_limit=UNLIMITED_STOCK_LIMIT,
            sold_count=stock.sold_count,
            stock_remaining=max(ready_available, 0),
            is_unlimited=False,
        )
    return PlanStockState(
        sales_limit=stock.sales_limit,
        sold_count=stock.sold_count,
        stock_remaining=min(stock.stock_remaining or 0, max(ready_available, 0)),
        is_unlimited=False,
    )
