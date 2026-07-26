"""Regression test for the apply_renewal(plan=...) signature mismatch.

Two callers — services/payment.py (gateway renewal) and
apps/bot/handlers/user/renewal.py (wallet/bot renewal) — invoke
services.renewal.apply_renewal with a keyword `plan=`. The function's
signature must accept it, otherwise every renewal on those paths raises
TypeError, hits the except/refund branch, and the renewal is never applied.

The functional tests mock apply_renewal, so they cannot catch this — this
test asserts against the REAL signature.
"""
from __future__ import annotations

import inspect

from services.renewal import apply_renewal

def test_apply_renewal_accepts_plan_keyword() -> None:
    params = inspect.signature(apply_renewal).parameters
    assert "plan" in params, (
        "apply_renewal must accept a `plan` keyword — payment.py and "
        "renewal.py callers pass plan=plan."
    )


def test_apply_renewal_plan_is_optional() -> None:
    """Callers that don't load a plan (auto_renew, admin gifts, mini-app,
    admin subs) must keep working, so `plan` needs a default."""
    plan_param = inspect.signature(apply_renewal).parameters["plan"]
    assert plan_param.default is None


def test_apply_renewal_binds_all_caller_kwargs() -> None:
    """The exact kwargs used by the gateway/bot renewal callers must bind."""
    sig = inspect.signature(apply_renewal)
    # Should not raise TypeError.
    sig.bind(
        session=object(),
        subscription=object(),
        renew_type="plan",
        amount=1.0,
        plan=object(),
    )


# ─── plan renewal: fresh-start (reset) semantics ─────────────────────────────
# "تمدید پلن فعلی" costs the FULL plan price, so quota AND days must RESET to
# the plan's values — never stacked on the remainder — and the panel's traffic
# counter must be zeroed via the reset_usage flag.

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.renewal


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Repo:
    """Stub AppSettingsRepository — apply_renewal fetches service security
    settings internally, and we don't want a real DB hit."""

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
    sub.status = kw.get("status", "active")
    sub.activated_at = kw.get("activated_at")
    sub.ends_at = kw.get("ends_at")
    sub.volume_bytes = kw.get("volume_bytes", 0)
    sub.used_bytes = kw.get("used_bytes", 0)
    sub.lifetime_used_bytes = kw.get("lifetime_used_bytes", 0)
    sub.plan_id = None
    return sub


@pytest.mark.asyncio
async def test_plan_renewal_resets_quota_and_days(monkeypatch):
    """Active sub mid-cycle: volume/days are REPLACED, usage zeroed, and the
    pre-renewal consumption rolls into lifetime_used_bytes."""
    now = datetime.now(timezone.utc)
    sub = _sub(
        activated_at=now - timedelta(days=10),
        ends_at=now + timedelta(days=20),           # 20 days still left
        volume_bytes=50 * 1024**3,
        used_bytes=30 * 1024**3,
        lifetime_used_bytes=5 * 1024**3,
    )
    plan = MagicMock(volume_bytes=100 * 1024**3, duration_days=30, name="Gold")
    session = _session(monkeypatch, xui_record=MagicMock())
    synced: dict = {}

    async def _fake_sync(session, subscription, xui, sec, *, plan=None, reset_usage=False):
        synced["reset_usage"] = reset_usage

    monkeypatch.setattr(services.renewal, "_sync_xui_limits", _fake_sync)

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="plan", amount=1, plan=plan,
    )

    assert sub.volume_bytes == 100 * 1024**3          # reset to plan quota, NOT 150GB
    assert sub.used_bytes == 0                        # usage zeroed
    assert sub.lifetime_used_bytes == 35 * 1024**3    # 5 + 30 rolled up
    # days RESET from now, NOT extended: must be ~now+30d, NOT now+50d
    assert abs((sub.ends_at - (now + timedelta(days=30))).total_seconds()) < 5
    assert abs((sub.starts_at - now).total_seconds()) < 5
    assert synced["reset_usage"] is True              # panel counter zeroed too


@pytest.mark.asyncio
async def test_plan_renewal_expired_sub_reactivates_from_now(monkeypatch):
    now = datetime.now(timezone.utc)
    sub = _sub(
        status="expired",
        activated_at=now - timedelta(days=40),
        ends_at=now - timedelta(days=10),
        volume_bytes=50 * 1024**3,
        used_bytes=50 * 1024**3,
    )
    plan = MagicMock(volume_bytes=20 * 1024**3, duration_days=7, name="Mini")
    session = _session(monkeypatch, xui_record=MagicMock())

    async def _fake_sync(*a, **kw):
        pass

    monkeypatch.setattr(services.renewal, "_sync_xui_limits", _fake_sync)

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="plan", amount=1, plan=plan,
    )

    assert sub.status == "active"
    assert sub.volume_bytes == 20 * 1024**3
    assert sub.used_bytes == 0
    assert sub.lifetime_used_bytes == 50 * 1024**3
    assert abs((sub.ends_at - (now + timedelta(days=7))).total_seconds()) < 5


@pytest.mark.asyncio
async def test_plan_renewal_unlimited_plan(monkeypatch):
    """duration_days=0 / volume_bytes=0 means unlimited: ends_at=None, quota=0."""
    now = datetime.now(timezone.utc)
    sub = _sub(
        activated_at=now - timedelta(days=3),
        ends_at=now + timedelta(days=5),
        volume_bytes=10 * 1024**3,
        used_bytes=2 * 1024**3,
    )
    plan = MagicMock(volume_bytes=0, duration_days=0, name="Unlimited")
    session = _session(monkeypatch, xui_record=MagicMock())

    async def _fake_sync(*a, **kw):
        pass

    monkeypatch.setattr(services.renewal, "_sync_xui_limits", _fake_sync)

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="plan", amount=1, plan=plan,
    )

    assert sub.ends_at is None                        # unlimited time
    assert sub.volume_bytes == 0                      # unlimited quota
    assert sub.used_bytes == 0


@pytest.mark.asyncio
async def test_volume_renewal_still_stacks_and_does_not_reset(monkeypatch):
    """Volume renewals must NOT change behaviour: quota grows, usage untouched,
    and no panel counter reset is requested."""
    now = datetime.now(timezone.utc)
    sub = _sub(
        activated_at=now - timedelta(days=5),
        ends_at=now + timedelta(days=25),
        volume_bytes=50 * 1024**3,
        used_bytes=30 * 1024**3,
    )
    session = _session(monkeypatch, xui_record=MagicMock())
    synced: dict = {}

    async def _fake_sync(session, subscription, xui, sec, *, plan=None, reset_usage=False):
        synced["reset_usage"] = reset_usage

    monkeypatch.setattr(services.renewal, "_sync_xui_limits", _fake_sync)

    await services.renewal.apply_renewal(
        session=session, subscription=sub, renew_type="volume", amount=40,
    )

    assert sub.volume_bytes == 90 * 1024**3           # 50 + 40 stacked
    assert sub.used_bytes == 30 * 1024**3             # untouched
    assert sub.lifetime_used_bytes == 0               # untouched
    assert synced["reset_usage"] is False             # NO reset for top-ups
