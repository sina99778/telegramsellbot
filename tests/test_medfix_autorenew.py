"""Regression tests for the medium-severity auto-renew fix (finding #50):

The per-subscription re-check inside the renewal lock must re-apply the FULL
eligibility predicate (auto_renew_enabled + status + the ends_at proximity
window) on FRESH data — a user who manually renewed between the candidate
scan and lock acquisition pushes ends_at out of the window and must NOT be
charged a second time.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _now():
    return datetime.now(timezone.utc)


def _make_sub(balance=Decimal("100.00")):
    wallet = NS(balance=balance)
    user = NS(id=uuid4(), wallet=wallet, is_bot_blocked=True, telegram_id=1)
    plan = NS(duration_days=30)
    return NS(id=uuid4(), plan_id=uuid4(), user=user, plan=plan, xui_client=None)


def _fresh_result(row):
    """A mock for `await session.execute(...)` whose .one_or_none() returns row."""
    result = MagicMock()
    result.one_or_none.return_value = row
    return result


@asynccontextmanager
async def _acquired_lock(key, ttl_seconds=60):
    yield True


async def _run(mock_session, sub, fresh_row):
    """Drive _try_auto_renew with the lock acquired and a given fresh DB row."""
    from apps.worker.jobs.auto_renew import _try_auto_renew

    mock_session.execute = AsyncMock(return_value=_fresh_result(fresh_row))
    wm = MagicMock()
    wm.process_transaction = AsyncMock()
    apply = AsyncMock()
    with patch("apps.worker.jobs.auto_renew.distributed_lock", _acquired_lock), \
         patch("apps.worker.jobs.auto_renew.calculate_renewal_price", return_value=Decimal("5.00")), \
         patch("apps.worker.jobs.auto_renew.WalletManager", return_value=wm), \
         patch("apps.worker.jobs.auto_renew.apply_renewal", apply):
        await _try_auto_renew(mock_session, MagicMock(), sub, MagicMock())
    return wm, apply


# ─── the core regression: manual renewal during the run must not double-charge ─


@pytest.mark.asyncio
async def test_manually_renewed_sub_is_not_charged_again(mock_session):
    sub = _make_sub()
    # Fresh row: a manual renewal just pushed ends_at 30 days out (way past
    # the 24h renewal window).
    fresh = NS(auto_renew_enabled=True, status="active", ends_at=_now() + timedelta(days=30))

    wm, apply = await _run(mock_session, sub, fresh)

    wm.process_transaction.assert_not_awaited()   # no debit
    apply.assert_not_awaited()                    # no panel extension
    mock_session.add.assert_not_called()          # no Order created
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sub_past_grace_window_is_not_charged(mock_session):
    sub = _make_sub()
    fresh = NS(auto_renew_enabled=True, status="expired", ends_at=_now() - timedelta(days=5))

    wm, apply = await _run(mock_session, sub, fresh)

    wm.process_transaction.assert_not_awaited()
    apply.assert_not_awaited()
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_auto_renew_toggled_off_inside_lock_skips(mock_session):
    sub = _make_sub()
    fresh = NS(auto_renew_enabled=False, status="active", ends_at=_now() + timedelta(hours=1))

    wm, apply = await _run(mock_session, sub, fresh)

    wm.process_transaction.assert_not_awaited()
    apply.assert_not_awaited()
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_sub_deleted_during_run_skips(mock_session):
    sub = _make_sub()

    wm, apply = await _run(mock_session, sub, None)  # row gone

    wm.process_transaction.assert_not_awaited()
    apply.assert_not_awaited()
    mock_session.add.assert_not_called()


# ─── still-eligible subs must keep renewing (no over-blocking) ────────────────


@pytest.mark.asyncio
async def test_still_eligible_sub_renews_normally(mock_session):
    sub = _make_sub()
    fresh = NS(auto_renew_enabled=True, status="active", ends_at=_now() + timedelta(hours=1))

    wm, apply = await _run(mock_session, sub, fresh)

    wm.process_transaction.assert_awaited_once()  # wallet debited once
    apply.assert_awaited_once()                   # panel extended
    mock_session.add.assert_called()              # Order persisted
    mock_session.commit.assert_awaited()
