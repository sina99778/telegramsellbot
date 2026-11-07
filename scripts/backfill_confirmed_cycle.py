"""Apply a display-only baseline from an independently verified private snapshot.

Run with --snapshot PATH to validate, then --apply to persist. This is never an
automatic migration: old plan sizes and missing purchase history cannot be guessed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory, dispose_database
from models.plan import Plan
from models.subscription import Subscription
from models.wallet import WalletTransaction


async def backfill_confirmed_cycle(
    session: AsyncSession, snapshot: dict[str, Any], *, apply: bool = False,
) -> int:
    sub = await session.scalar(
        select(Subscription)
        .where(Subscription.id == UUID(snapshot["subscription_id"]))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if sub is None:
        raise ValueError("Subscription no longer exists")
    current = {
        "owner_id": str(sub.user_id), "plan_id": str(sub.plan_id),
        "order_id": str(sub.order_id), "starts_at": sub.starts_at.isoformat()
        if sub.starts_at else None,
        "ends_at": sub.ends_at.isoformat() if sub.ends_at else None,
        "volume_bytes": sub.volume_bytes, "lifetime_used_bytes": sub.lifetime_used_bytes,
    }
    if any(snapshot[key] != value for key, value in current.items()):
        raise ValueError("Historical cycle changed; refusing an inferred correction")
    plan = await session.scalar(select(Plan).where(Plan.id == sub.plan_id))
    quota = int(snapshot["plan_quota_bytes"])
    if plan is None or quota <= 0 or plan.volume_bytes != quota:
        raise ValueError("Confirmed plan quota changed")
    tx = await session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.id == UUID(snapshot["renewal_tx_id"]),
            WalletTransaction.user_id == sub.user_id,
            WalletTransaction.type == "renewal", WalletTransaction.direction == "debit",
            WalletTransaction.metadata_["sub_id"].astext == str(sub.id),
        )
    )
    if (
        tx is None or tx.reference_id != sub.order_id
        or (tx.metadata_ or {}).get("type") != "plan"
        or sub.starts_at is None
        or abs((sub.starts_at - tx.created_at).total_seconds()) >= 10
    ):
        raise ValueError("Confirmed full-plan renewal is not verifiable")
    later: UUID | None = await session.scalar(
        select(WalletTransaction.id).where(
            WalletTransaction.user_id == sub.user_id,
            WalletTransaction.type == "renewal", WalletTransaction.direction == "debit",
            WalletTransaction.metadata_["sub_id"].astext == str(sub.id),
            WalletTransaction.created_at > tx.created_at,
        ).limit(1)
    )
    baseline = int(snapshot["cycle_start_used_bytes"])
    if (
        later is not None or baseline != sub.lifetime_used_bytes + sub.volume_bytes - quota
        or not 0 <= baseline <= sub.lifetime_used_bytes + sub.used_bytes
        or sub.cycle_start_used_bytes not in (None, baseline)
    ):
        raise ValueError("Cycle baseline no longer matches confirmed evidence")
    if apply and sub.cycle_start_used_bytes is None:
        sub.cycle_start_used_bytes = baseline
        await session.flush()
    return baseline


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    try:
        async with AsyncSessionFactory() as session:
            baseline = await backfill_confirmed_cycle(session, snapshot, apply=args.apply)
            if args.apply:
                await session.commit()
        print(json.dumps({"validated": True, "applied": args.apply, "baseline_bytes": baseline}))
    finally:
        await dispose_database()


if __name__ == "__main__":
    asyncio.run(main())
