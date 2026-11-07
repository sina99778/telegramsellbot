"""A renewal's displayed quota is independent of the cumulative panel cap."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

GIB = 1024**3


@pytest.mark.parametrize(
    "volume,used,lifetime,baseline,expected",
    [
        (40, 20, 1, 21, (20, 0, 20)),
        (40, 23, 1, 21, (20, 3, 17)),
        (50, 23, 1, 21, (30, 3, 27)),
        (17, 0, 24, 21, (20, 3, 17)),
        (43, 23, 1, 24, (20, 0, 20)),
        (0, 23, 1, 21, (0, 3, 0)),
        (40, 20, 10, None, (40, 20, 20)),
        (20, 22, 0, None, (20, 22, 0)),
        (40, 42, 1, 21, (20, 22, 0)),
    ],
)
def test_display_cycle_preserves_remaining_and_migration(
    volume, used, lifetime, baseline, expected,
):
    from services.subscription_usage import display_usage

    subscription = NS(
        volume_bytes=volume * GIB, used_bytes=used * GIB,
        lifetime_used_bytes=lifetime * GIB,
        cycle_start_used_bytes=baseline * GIB if baseline is not None else None,
    )
    before = vars(subscription).copy()
    result = display_usage(subscription)
    assert (result.total_bytes, result.used_bytes, result.remaining_bytes) == tuple(
        number * GIB for number in expected
    )
    assert vars(subscription) == before


async def test_full_plan_reset_records_the_lifetime_baseline_atomically(mock_session):
    from services import renewal

    sub = NS(
        id=uuid4(), user_id=uuid4(), status="active", plan_id=uuid4(),
        volume_bytes=40 * GIB, used_bytes=20 * GIB, lifetime_used_bytes=GIB,
        cycle_start_used_bytes=None, panel_sync_pending=False,
        ends_at=datetime.now(UTC) + timedelta(days=5),
    )
    plan = NS(volume_bytes=20 * GIB, duration_days=30)
    mock_session.no_autoflush = MagicMock()
    mock_session.scalar.return_value = NS(
        inbound=NS(server=NS(credentials=NS())),
        xui_client_remote_id="fixture", client_uuid="fixture",
    )
    with patch.object(renewal, "record_is_marzban_family", return_value=False):
        await renewal.apply_renewal(
            session=mock_session, subscription=sub, renew_type="plan", amount=1, plan=plan,
        )
    assert sub.cycle_start_used_bytes == 21 * GIB
    assert sub.volume_bytes == 40 * GIB and sub.used_bytes == 20 * GIB
    assert sub.lifetime_used_bytes == GIB and sub.panel_sync_pending
    mock_session.commit.assert_not_awaited()


@pytest.mark.parametrize("renew_type", ["volume", "time"])
async def test_additions_keep_the_current_cycle_baseline(mock_session, renew_type):
    from services import renewal
    from services.subscription_usage import display_usage

    sub = NS(
        id=uuid4(), user_id=uuid4(), status="active", plan_id=uuid4(),
        volume_bytes=40 * GIB, used_bytes=20 * GIB, lifetime_used_bytes=GIB,
        cycle_start_used_bytes=21 * GIB, panel_sync_pending=False,
        ends_at=datetime.now(UTC) + timedelta(days=5),
    )
    mock_session.no_autoflush = MagicMock()
    mock_session.scalar.return_value = NS(
        inbound=NS(server=NS(credentials=NS())),
        xui_client_remote_id="fixture", client_uuid="fixture",
    )
    with patch.object(renewal, "record_is_marzban_family", return_value=False):
        await renewal.apply_renewal(
            session=mock_session, subscription=sub, renew_type=renew_type, amount=5,
            plan=NS(volume_bytes=20 * GIB, duration_days=30),
        )
    assert sub.cycle_start_used_bytes == 21 * GIB
    assert sub.lifetime_used_bytes == GIB and sub.used_bytes == 20 * GIB
    assert display_usage(sub).used_bytes == 0
    assert display_usage(sub).total_bytes == (25 if renew_type == "volume" else 20) * GIB
