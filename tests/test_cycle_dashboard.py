"""Dashboard service rows share the customer's renewal-cycle display."""

from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from uuid import uuid4


async def test_dashboard_subscription_rows_show_cycle_usage(mock_session):
    from apps.api.routes.dashboard.users import user_detail

    gib = 1024**3
    user = MagicMock(id=uuid4(), wallet=None, created_at=None, last_seen_at=None)
    sub = NS(
        id=uuid4(), source=None, xui_client=None, status="active", sub_link=None,
        panel_sync_pending=False, panel_sync_error=None, cycle_start_used_bytes=21 * gib,
        volume_bytes=40 * gib, used_bytes=20 * gib, lifetime_used_bytes=gib,
        starts_at=None, ends_at=None, created_at=None,
    )
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [sub]
    mock_session.scalar.return_value = user
    mock_session.execute.return_value = rows
    response = await user_detail(user.id, (MagicMock(), mock_session))
    displayed = response["subscriptions"][0]
    assert displayed["volume_bytes"] == 20 * gib
    assert displayed["used_bytes"] == 0
    assert displayed["lifetime_used_bytes"] == gib
    assert sub.volume_bytes == 40 * gib and sub.used_bytes == 20 * gib
