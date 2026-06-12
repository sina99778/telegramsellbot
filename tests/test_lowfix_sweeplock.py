"""
Regression tests for finding #79: the 48h waiting_hash expiry sweep must not
overwrite a concurrently-approved payment's 'finished' status with 'expired'.

The old cleanup did an unlocked bulk read-modify-write: it SELECTed all
waiting_hash payments older than 48h without FOR UPDATE, set
payment_status='expired' on the loaded objects and committed. If an admin
approval (which holds the row FOR UPDATE across process_successful_payment)
committed in between, the sweep's stale write won — flipping a finished,
wallet-credited payment to 'expired'.

The fix mirrors the per-row pattern used by the gateway sweep above it and by
card_autoconfirm: list candidate ids without a lock, then per row take
with_for_update(skip_locked=True) with the eligibility (payment_status ==
'waiting_hash' AND actually_paid IS NULL) re-checked inside the locked
SELECT's WHERE clause.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.worker.jobs.payments import sync_pending_payments


def _session_cm(session):
    """Wrap a mock session in an async context manager like AsyncSessionFactory()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _empty_result():
    result = MagicMock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    return result


def _ids_result(ids):
    result = MagicMock()
    result.all.return_value = [(i,) for i in ids]
    return result


def _gateway_list_session():
    """Session for the first (gateway sweep) listing query — returns nothing."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_empty_result())
    return session


async def _run_sweep(sessions):
    with patch(
        "apps.worker.jobs.payments.AsyncSessionFactory",
        side_effect=[_session_cm(s) for s in sessions],
    ), patch("apps.worker.jobs.payments.review_gateway_payment", AsyncMock()):
        await sync_pending_payments()


# ─── per-row locking behaviour ───────────────────────────────────────────


async def test_stale_waiting_hash_payment_is_expired_under_row_lock(make_payment):
    payment = make_payment(provider="card_to_card", payment_status="waiting_hash")

    cleanup_list_session = AsyncMock()
    cleanup_list_session.execute = AsyncMock(return_value=_ids_result([payment.id]))

    row_session = AsyncMock()
    row_session.scalar = AsyncMock(return_value=payment)

    await _run_sweep([_gateway_list_session(), cleanup_list_session, row_session])

    assert payment.payment_status == "expired"
    row_session.commit.assert_awaited_once()

    # The per-row SELECT must take the row lock with skip_locked and re-check
    # eligibility in its WHERE clause (status + not-credited), so a stale
    # candidate can never be expired after a concurrent approval commits.
    stmt = row_session.scalar.await_args.args[0]
    assert stmt._for_update_arg is not None  # SELECT ... FOR UPDATE
    assert stmt._for_update_arg.skip_locked is True
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "payments.payment_status = 'waiting_hash'" in sql
    assert "payments.actually_paid IS NULL" in sql


async def test_skips_payment_approved_between_listing_and_locking(make_payment):
    # The locked re-checking SELECT returns None when the row no longer
    # matches (e.g. an admin approval set status='finished' + actually_paid)
    # or is currently locked by the approval transaction (skip_locked).
    finished = make_payment(
        provider="card_to_card", payment_status="finished", actually_paid="5.00"
    )

    cleanup_list_session = AsyncMock()
    cleanup_list_session.execute = AsyncMock(return_value=_ids_result([finished.id]))

    row_session = AsyncMock()
    row_session.scalar = AsyncMock(return_value=None)  # WHERE re-check filtered it out

    await _run_sweep([_gateway_list_session(), cleanup_list_session, row_session])

    # Nothing written, nothing committed — the finished payment keeps its status.
    assert finished.payment_status == "finished"
    row_session.commit.assert_not_awaited()


async def test_one_failing_row_does_not_abort_rest_of_cleanup(make_payment):
    p1 = make_payment(provider="card_to_card", payment_status="waiting_hash")
    p2 = make_payment(provider="card_to_card", payment_status="waiting_hash")
    p2.id = uuid4()  # the make_payment fixture reuses one payment_id per test

    cleanup_list_session = AsyncMock()
    cleanup_list_session.execute = AsyncMock(return_value=_ids_result([p1.id, p2.id]))

    failing_session = AsyncMock()
    failing_session.scalar = AsyncMock(return_value=p1)
    failing_session.commit = AsyncMock(side_effect=RuntimeError("commit failed"))

    ok_session = AsyncMock()
    ok_session.scalar = AsyncMock(return_value=p2)

    await _run_sweep(
        [_gateway_list_session(), cleanup_list_session, failing_session, ok_session]
    )

    failing_session.rollback.assert_awaited_once()
    assert p2.payment_status == "expired"
    ok_session.commit.assert_awaited_once()


async def test_cleanup_listing_query_selects_only_ids_without_lock():
    cleanup_list_session = AsyncMock()
    cleanup_list_session.execute = AsyncMock(return_value=_ids_result([]))

    await _run_sweep([_gateway_list_session(), cleanup_list_session])

    stmt = cleanup_list_session.execute.await_args.args[0]
    sql = str(stmt)
    # Phase 1 is an id-only, lock-free listing — never a row-object load that
    # could later be committed over a concurrently-updated row.
    assert "FOR UPDATE" not in sql
    assert sql.upper().startswith("SELECT PAYMENTS.ID")
    assert "payments.payment_status =" in sql
    assert "payments.created_at <" in sql
