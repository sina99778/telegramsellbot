"""Tests for the renewal-safety fixes: pending-time block, fractional reject,
and the canonical cross-surface lock key."""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core.redis import renewal_lock_key
from services.renewal import (
    PENDING_TIME_RENEWAL_MSG,
    RenewalNotAllowedError,
    apply_renewal,
    time_renewal_blocked,
)


@pytest.mark.parametrize(
    "status,renew_type,expected",
    [
        ("pending_activation", "time", True),
        ("active", "time", False),
        ("expired", "time", False),
        ("pending_activation", "volume", False),  # volume is fine while pending
        ("active", "volume", False),
    ],
)
def test_time_renewal_blocked(status, renew_type, expected):
    assert time_renewal_blocked(NS(status=status), renew_type) is expected


def test_renewal_lock_key_is_subscription_scoped():
    sid = uuid4()
    # Keyed ONLY on the sub id → every surface (bot/mini-app/worker) computes the
    # SAME key and mutually excludes. It must NOT vary by telegram/user id.
    assert renewal_lock_key(sid) == f"renewal_lock:{sid}"
    assert renewal_lock_key(sid) == renewal_lock_key(sid)


@pytest.mark.asyncio
async def test_apply_renewal_blocks_pending_time():
    # The safety net must reject before any DB/panel work (so no money is taken
    # for days that would be discarded on first connect).
    sub = NS(status="pending_activation", id=uuid4())
    with pytest.raises(RenewalNotAllowedError) as ei:
        await apply_renewal(session=MagicMock(), subscription=sub, renew_type="time", amount=10)
    assert str(ei.value) == PENDING_TIME_RENEWAL_MSG
