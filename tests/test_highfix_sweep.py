"""
Regression tests for finding #21: Tronado payments must NOT drop out of the
pending-payment sweep after the first unpaid review.

_review_tronado_payment overwrites payment_status with the provider's
free-form OrderStatusTitle (or the "not_paid" fallback) on the IsPaid=False
branch, which is outside the NowPayments-vocabulary set the sweep used to
filter on. The fix sweeps Tronado rows by EXCLUSION: any status outside
TRONADO_TERMINAL_STATUSES (time-bounded by created_at) stays in the sweep.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.worker.jobs.payments import (
    PENDING_GATEWAY_STATUSES,
    TRONADO_TERMINAL_STATUSES,
    _is_sweepable,
    sync_pending_payments,
)


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


# ─── _is_sweepable unit tests ────────────────────────────────────────────


@pytest.mark.parametrize("status", list(PENDING_GATEWAY_STATUSES))
def test_pending_gateway_statuses_sweepable_for_any_provider(make_payment, status):
    assert _is_sweepable(make_payment(provider="nowpayments", payment_status=status))
    assert _is_sweepable(make_payment(provider="tronado", payment_status=status))


@pytest.mark.parametrize("status", ["not_paid", "pending payment", "در انتظار پرداخت"])
def test_tronado_provider_title_statuses_stay_sweepable(make_payment, status):
    # The exact bug: after one unpaid review the status becomes the provider
    # display title or 'not_paid' — the payment must remain in the sweep.
    assert _is_sweepable(make_payment(provider="tronado", payment_status=status))


@pytest.mark.parametrize("status", list(TRONADO_TERMINAL_STATUSES))
def test_tronado_terminal_statuses_not_sweepable(make_payment, status):
    assert not _is_sweepable(make_payment(provider="tronado", payment_status=status))


def test_exclusion_rule_only_applies_to_tronado(make_payment):
    # Other providers keep the strict pending vocabulary.
    assert not _is_sweepable(make_payment(provider="nowpayments", payment_status="not_paid"))
    assert not _is_sweepable(make_payment(provider="tetrapay", payment_status="not_paid"))


# ─── sweep behaviour ─────────────────────────────────────────────────────


async def test_sweep_reviews_tronado_payment_stuck_in_not_paid(make_payment):
    payment = make_payment(provider="tronado", payment_status="not_paid", order_id="TRX-1")

    list_session = AsyncMock()
    list_result = MagicMock()
    list_result.all.return_value = [(payment.id,)]
    list_session.execute = AsyncMock(return_value=list_result)

    row_session = AsyncMock()
    row_session.scalar = AsyncMock(return_value=payment)

    cleanup_session = AsyncMock()
    cleanup_session.execute = AsyncMock(return_value=_empty_result())

    sessions = [_session_cm(s) for s in (list_session, row_session, cleanup_session)]
    review = AsyncMock(return_value="not_paid")
    with patch("apps.worker.jobs.payments.AsyncSessionFactory", side_effect=sessions), \
         patch("apps.worker.jobs.payments.review_gateway_payment", review):
        await sync_pending_payments()

    # The stuck payment was re-reviewed (missed-IPN recovery) and committed.
    review.assert_awaited_once()
    assert review.await_args.args[1] is payment
    row_session.commit.assert_awaited_once()


async def test_sweep_skips_payment_that_turned_terminal_after_listing(make_payment):
    # Status changed between listing and locking (e.g. the IPN finished it).
    payment = make_payment(provider="tronado", payment_status="finished", order_id="TRX-2")

    list_session = AsyncMock()
    list_result = MagicMock()
    list_result.all.return_value = [(payment.id,)]
    list_session.execute = AsyncMock(return_value=list_result)

    row_session = AsyncMock()
    row_session.scalar = AsyncMock(return_value=payment)

    cleanup_session = AsyncMock()
    cleanup_session.execute = AsyncMock(return_value=_empty_result())

    sessions = [_session_cm(s) for s in (list_session, row_session, cleanup_session)]
    review = AsyncMock(return_value="processed")
    with patch("apps.worker.jobs.payments.AsyncSessionFactory", side_effect=sessions), \
         patch("apps.worker.jobs.payments.review_gateway_payment", review):
        await sync_pending_payments()

    review.assert_not_awaited()


async def test_sweep_query_selects_tronado_by_exclusion():
    list_session = AsyncMock()
    list_session.execute = AsyncMock(return_value=_empty_result())

    cleanup_session = AsyncMock()
    cleanup_session.execute = AsyncMock(return_value=_empty_result())

    sessions = [_session_cm(s) for s in (list_session, cleanup_session)]
    with patch("apps.worker.jobs.payments.AsyncSessionFactory", side_effect=sessions):
        await sync_pending_payments()

    stmt = list_session.execute.await_args_list[0].args[0]
    sql = str(stmt)
    # OR-branch: tronado rows selected by status EXCLUSION + recency bound.
    assert " OR " in sql
    assert "NOT IN" in sql
    assert "payments.created_at >=" in sql
    assert "payments.provider =" in sql
