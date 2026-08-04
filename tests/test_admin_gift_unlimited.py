"""Tests for the admin gift (bandwidth / time) fixes.

Three bugs, all on the path an admin uses to *gift* GB or days:

1. Gifting GB to an UNLIMITED config (volume_bytes == 0) ran
   `volume_bytes += amount*1024**3`, turning 0 into a finite cap — a silent
   DOWNGRADE from unlimited to metered.
2. Gifting days to a never-expiring config (ends_at is None on an active sub)
   wrote a concrete date, CAPPING a config that previously never expired.
3. The bulk gift path did not take the renewal lock the single-subscription
   admin handlers take, so a gift racing a paid renewal could lose an update.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import services.renewal


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Repo:
    """Stub AppSettingsRepository — apply_renewal fetches service security
    settings internally and we don't want a real DB hit."""

    def __init__(self, session):
        pass

    async def get_service_security_settings(self):
        return MagicMock(xui_limit_ip=0)


def _session(monkeypatch, xui_record=None):
    monkeypatch.setattr(services.renewal, "AppSettingsRepository", _Repo)
    s = MagicMock()
    s.begin_nested = MagicMock(return_value=_Nested())
    s.flush = AsyncMock()
    s.scalar = AsyncMock(return_value=xui_record)
    s.execute = AsyncMock()
    return s


def _sub(**kw):
    sub = MagicMock()
    sub.id = kw.get("id", uuid4())
    sub.status = kw.get("status", "active")
    sub.activated_at = kw.get("activated_at")
    sub.ends_at = kw.get("ends_at")
    sub.volume_bytes = kw.get("volume_bytes", 0)
    sub.used_bytes = kw.get("used_bytes", 0)
    sub.lifetime_used_bytes = kw.get("lifetime_used_bytes", 0)
    sub.plan_id = None
    return sub


@pytest.fixture
def no_panel_sync(monkeypatch):
    async def _fake_sync(*a, **kw):
        pass

    monkeypatch.setattr(services.renewal, "_sync_xui_limits", _fake_sync)


# ── Bug 1: volume gift must not cap an unlimited config ──────────────────────


@pytest.mark.asyncio
async def test_volume_gift_leaves_unlimited_config_unlimited(monkeypatch, no_panel_sync):
    """volume_bytes == 0 means UNLIMITED. Gifting 50GB must NOT turn it into a
    50GB cap — that would be a downgrade disguised as a gift."""
    now = datetime.now(timezone.utc)
    sub = _sub(ends_at=now + timedelta(days=30), volume_bytes=0, used_bytes=7 * 1024**3)
    session = _session(monkeypatch, xui_record=MagicMock())

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="volume", amount=50,
    )

    assert sub.volume_bytes == 0, (
        "Gifting GB to an unlimited config must leave it unlimited, "
        f"but volume_bytes became {sub.volume_bytes}."
    )
    assert sub.used_bytes == 7 * 1024**3  # untouched


@pytest.mark.asyncio
async def test_volume_gift_still_stacks_on_metered_config(monkeypatch, no_panel_sync):
    """The guard must be narrow: a normal metered config still gains the GB."""
    now = datetime.now(timezone.utc)
    sub = _sub(ends_at=now + timedelta(days=30), volume_bytes=20 * 1024**3)
    session = _session(monkeypatch, xui_record=MagicMock())

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="volume", amount=30,
    )

    assert sub.volume_bytes == 50 * 1024**3


# ── Bug 2: time gift must not cap a never-expiring config ────────────────────


@pytest.mark.asyncio
async def test_time_gift_leaves_never_expiring_config_unexpiring(monkeypatch, no_panel_sync):
    """ends_at is None on an ACTIVE config means unlimited duration (the
    pending_activation case is rejected earlier by time_renewal_blocked).
    Gifting days must not write a concrete expiry date."""
    sub = _sub(status="active", ends_at=None)
    session = _session(monkeypatch, xui_record=MagicMock())

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="time", amount=30,
    )

    assert sub.ends_at is None, (
        "Gifting days to a never-expiring config must leave it never-expiring, "
        f"but ends_at became {sub.ends_at}."
    )


@pytest.mark.asyncio
async def test_time_gift_extends_active_config(monkeypatch, no_panel_sync):
    """Narrowness check: a normal dated config still gets its days."""
    now = datetime.now(timezone.utc)
    ends = now + timedelta(days=10)
    sub = _sub(status="active", ends_at=ends)
    session = _session(monkeypatch, xui_record=MagicMock())

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="time", amount=5,
    )

    assert abs((sub.ends_at - (ends + timedelta(days=5))).total_seconds()) < 5


@pytest.mark.asyncio
async def test_time_gift_on_expired_config_restarts_from_now(monkeypatch, no_panel_sync):
    """An expired config must restart from now, not from its stale past date."""
    now = datetime.now(timezone.utc)
    sub = _sub(status="expired", ends_at=now - timedelta(days=10))
    session = _session(monkeypatch, xui_record=MagicMock())

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="time", amount=7,
    )

    assert abs((sub.ends_at - (now + timedelta(days=7))).total_seconds()) < 5
    assert sub.status == "active"


@pytest.mark.asyncio
async def test_time_gift_on_pending_activation_is_still_rejected(monkeypatch, no_panel_sync):
    """The `ends_at is None` no-op must NOT have swallowed the pre-existing
    pending_activation guard: a not-yet-activated config also has ends_at None,
    and gifting time there must still raise (the days would be discarded on
    first connect). This is what makes the no-op safe."""
    sub = _sub(status="pending_activation", ends_at=None)
    session = _session(monkeypatch, xui_record=MagicMock())

    with pytest.raises(services.renewal.RenewalNotAllowedError):
        await services.renewal.apply_renewal(
            session=session, subscription=sub, renew_type="time", amount=30,
        )


# ── Bug 3: the bulk gift path must take the renewal lock ─────────────────────


@pytest.mark.asyncio
async def test_bulk_gift_skips_when_renewal_lock_is_held(monkeypatch):
    """A gift racing a paid renewal on the same subscription must back off
    rather than interleave two read-modify-write cycles and lose one."""
    import services.admin_gifts as ag

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock_held(key, ttl_seconds=30):
        yield False  # someone else holds it

    monkeypatch.setattr(ag, "distributed_lock", _lock_held)

    applied: list = []

    async def _spy_apply(**kw):
        applied.append(kw)

    monkeypatch.setattr(ag, "apply_renewal", _spy_apply)

    # A fully working session MUST be in place, otherwise this test passes
    # vacuously: without it, dropping the lock check makes the real session
    # factory raise, the broad `except` returns (False, None, None) anyway,
    # and the assertions below can never distinguish "skipped by the lock"
    # from "blew up". With a healthy session, the ONLY way to reach a False
    # result is the lock guard.
    sub = MagicMock()
    sub.user = MagicMock(telegram_id=999)
    sub.xui_client = MagicMock(username="cfg-z")

    session = MagicMock()
    session.scalar = AsyncMock(return_value=sub)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(ag, "AsyncSessionFactory", MagicMock(return_value=session))

    ok, tg_id, name = await ag._gift_single_subscription(uuid4(), "volume", 10)

    assert ok is False
    assert (tg_id, name) == (None, None)
    assert applied == [], "apply_renewal must not run without the lock."
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_gift_applies_and_commits_inside_the_lock(monkeypatch):
    """Happy path: lock acquired → renewal applied → committed. The commit must
    happen while the lock is still held, otherwise the gap is still racy."""
    import services.admin_gifts as ag

    from contextlib import asynccontextmanager

    events: list[str] = []

    @asynccontextmanager
    async def _lock_ok(key, ttl_seconds=30):
        events.append("lock_acquired")
        try:
            yield True
        finally:
            events.append("lock_released")

    monkeypatch.setattr(ag, "distributed_lock", _lock_ok)

    sub = MagicMock()
    sub.user = MagicMock(telegram_id=555)
    sub.xui_client = MagicMock(username="cfg-a")

    session = MagicMock()
    session.scalar = AsyncMock(return_value=sub)

    async def _commit():
        events.append("commit")

    session.commit = _commit
    session.rollback = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(ag, "AsyncSessionFactory", MagicMock(return_value=session))

    async def _spy_apply(**kw):
        events.append("apply_renewal")

    monkeypatch.setattr(ag, "apply_renewal", _spy_apply)

    ok, tg_id, name = await ag._gift_single_subscription(uuid4(), "time", 5)

    assert (ok, tg_id, name) == (True, 555, "cfg-a")
    # Ordering is the whole point of the fix.
    assert events == [
        "lock_acquired",
        "apply_renewal",
        "commit",
        "lock_released",
    ], f"commit must land inside the lock, got {events}"
