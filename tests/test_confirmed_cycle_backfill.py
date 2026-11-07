"""Only an independently confirmed, unchanged historical cycle can be backfilled."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def fixture():
    now = datetime.now(UTC)
    sub = NS(
        id=uuid4(), user_id=uuid4(), plan_id=uuid4(), order_id=uuid4(),
        starts_at=now, ends_at=now + timedelta(days=30), volume_bytes=40,
        used_bytes=21, lifetime_used_bytes=1, cycle_start_used_bytes=None,
    )
    tx = NS(id=uuid4(), reference_id=sub.order_id, created_at=now,
            metadata_={"type": "plan", "sub_id": str(sub.id)})
    snapshot = {
        "subscription_id": str(sub.id), "owner_id": str(sub.user_id),
        "plan_id": str(sub.plan_id), "order_id": str(sub.order_id),
        "starts_at": sub.starts_at.isoformat(), "ends_at": sub.ends_at.isoformat(),
        "volume_bytes": 40, "lifetime_used_bytes": 1, "plan_quota_bytes": 20,
        "cycle_start_used_bytes": 21, "renewal_tx_id": str(tx.id),
    }
    return sub, tx, snapshot


async def test_confirmed_backfill_changes_only_display_baseline():
    from scripts.backfill_confirmed_cycle import backfill_confirmed_cycle

    sub, tx, snapshot = fixture()
    session = AsyncMock()
    session.scalar.side_effect = [sub, NS(volume_bytes=20), tx, None]
    before = vars(sub).copy()
    baseline = await backfill_confirmed_cycle(session, snapshot, apply=True)
    assert baseline == 21 and sub.cycle_start_used_bytes == 21
    assert vars(sub) == before | {"cycle_start_used_bytes": 21}
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("changed", ["volume_bytes", "order_id", "lifetime_used_bytes"])
async def test_changed_historical_cycle_is_rejected(changed):
    from scripts.backfill_confirmed_cycle import backfill_confirmed_cycle

    sub, tx, snapshot = fixture()
    setattr(sub, changed, uuid4() if changed == "order_id" else 99)
    session = AsyncMock()
    session.scalar.side_effect = [sub, NS(volume_bytes=20), tx, None]
    with pytest.raises(ValueError):
        await backfill_confirmed_cycle(session, snapshot, apply=True)
    assert sub.cycle_start_used_bytes is None


async def test_dry_run_does_not_set_the_baseline():
    from scripts.backfill_confirmed_cycle import backfill_confirmed_cycle

    sub, tx, snapshot = fixture()
    session = AsyncMock()
    session.scalar.side_effect = [sub, NS(volume_bytes=20), tx, None]
    assert await backfill_confirmed_cycle(session, snapshot) == 21
    assert sub.cycle_start_used_bytes is None


async def test_later_renewal_blocks_historical_backfill():
    from scripts.backfill_confirmed_cycle import backfill_confirmed_cycle

    sub, tx, snapshot = fixture()
    session = AsyncMock()
    session.scalar.side_effect = [sub, NS(volume_bytes=20), tx, uuid4()]
    with pytest.raises(ValueError):
        await backfill_confirmed_cycle(session, snapshot, apply=True)
    assert sub.cycle_start_used_bytes is None
