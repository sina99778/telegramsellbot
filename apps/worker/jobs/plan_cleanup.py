"""Periodic cleanup of orphaned custom-purchase plans.

Every custom (دلخواه) purchase mints a one-off ``Plan`` row
(``services/custom_purchase.py::create_custom_purchase_plan``, code prefixed
``custom_``). In the bot flow that plan is **committed** the moment the user is
asked for a config name — *before* they pay (see
``apps/bot/handlers/user/purchase.py``). If the user then abandons the purchase,
the plan lingers forever with no order and no subscription pointing at it,
slowly bloating the ``plans`` table.

This job removes only the genuinely-orphaned ones: prefix ``custom_``, older than
a grace window, and referenced by **no order and no subscription**.

Safety:
  * The order check is mandatory — ``Order.plan_id`` is ``ondelete=RESTRICT``, so
    deleting a plan that still has an order would raise an IntegrityError *and*
    that plan represents a real purchase we must keep.
  * The subscription check is defence-in-depth — its FK is ``SET NULL``, so a
    blind delete would silently null a live subscription's ``plan_id``.
  * The grace window is comfortably longer than any payment can stay pending, so
    we never delete a plan that a still-pending payment will provision against.

Custom plans are already excluded from every user-facing plan list via
``not_(Plan.code.like("custom\\_%"))``, so they never clutter the UI — this is
purely about table growth.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import Delete, delete, select

from core.database import AsyncSessionFactory, utcnow
from models.order import Order
from models.plan import Plan
from models.subscription import Subscription

logger = logging.getLogger(__name__)

# A custom plan with no order/subscription older than this is abandoned. Kept well
# above any payment-pending window so a slow (e.g. crypto) payment is never cut off.
ORPHAN_GRACE_DAYS = 7


def build_orphan_custom_plan_delete(cutoff) -> Delete:
    """The DELETE that removes abandoned custom plans (extracted for testing)."""
    has_order = select(Order.id).where(Order.plan_id == Plan.id).exists()
    has_subscription = select(Subscription.id).where(Subscription.plan_id == Plan.id).exists()
    return (
        delete(Plan)
        .where(
            Plan.code.like("custom\\_%", escape="\\"),
            Plan.created_at < cutoff,
            ~has_order,
            ~has_subscription,
        )
        .execution_options(synchronize_session=False)
    )


async def cleanup_orphaned_custom_plans() -> int:
    """Delete abandoned custom-purchase plans. Returns the number removed."""
    cutoff = utcnow() - timedelta(days=ORPHAN_GRACE_DAYS)
    async with AsyncSessionFactory() as session:
        result = await session.execute(build_orphan_custom_plan_delete(cutoff))
        await session.commit()
        removed = result.rowcount or 0
        if removed:
            logger.info("[PLAN-CLEANUP] removed %d orphaned custom plan(s)", removed)
        return removed
