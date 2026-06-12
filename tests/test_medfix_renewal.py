"""Regression tests for finding 48: migrated (plan-less) configs were invoiced
with the average-rate price and offered the wallet button, but renew_pay_wallet
then hard-refused with 'پلن این سرویس حذف شده'. The wallet path must now work
for plan-less subs exactly like the gateway path: no Order row (Order.plan_id
is NOT NULL), the wallet ledger references the subscription instead."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import apps.bot.handlers.user.renewal as rmod
from apps.bot.handlers.user.renewal import RenewPayCallback, renew_pay_wallet
from models.order import Order


@asynccontextmanager
async def _acquired_lock(key, ttl_seconds=60):
    yield True


def _make_callback():
    loading_msg = MagicMock()
    loading_msg.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = NS(id=12345)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock(return_value=loading_msg)
    callback.message.delete = AsyncMock()
    return callback, loading_msg


def _make_sub(plan_id):
    sub = MagicMock()
    sub.id = uuid4()
    sub.status = "active"
    sub.plan_id = plan_id
    sub.ends_at = datetime.now(timezone.utc) + timedelta(days=30)
    return sub


def _make_rd(sub):
    user = NS(
        id=uuid4(),
        telegram_id=12345,
        username=None,
        first_name="x",
        wallet=NS(balance=Decimal("100")),
    )
    return {
        "sub_id": sub.id,
        "renew_type": "volume",
        "amount": 10.0,
        "price": Decimal("5.00"),
        "user": user,
    }


def _patches(rd, apply_renewal_mock=None):
    """Patch every collaborator of renew_pay_wallet so only its own control
    flow (order creation + ledger references) is under test."""
    pm_instance = MagicMock()
    pm_instance.preflight_check_subscription = AsyncMock(return_value=(True, None))
    wm_instance = MagicMock()
    wm_instance.process_transaction = AsyncMock()
    return (
        patch.object(rmod, "_get_renewal_data", AsyncMock(return_value=rd)),
        patch.object(rmod, "distributed_lock", _acquired_lock),
        patch.object(rmod, "_apply_renewal", apply_renewal_mock or AsyncMock()),
        patch.object(rmod, "_clear_sub_alert_keys", AsyncMock()),
        patch.object(rmod, "_notify_renewal_admins", AsyncMock()),
        patch.object(rmod, "safe_edit_or_send", AsyncMock()),
        patch("services.provisioning.manager.ProvisioningManager", return_value=pm_instance),
        patch("services.wallet.manager.WalletManager", return_value=wm_instance),
        wm_instance,
    )


def _cb_data(sub):
    return RenewPayCallback(m="w", s=sub.id.hex, t="v", a="10")


@pytest.mark.asyncio
async def test_wallet_renewal_proceeds_for_planless_sub(mock_session):
    """Plan-less (migrated) sub + sufficient balance: the wallet path must
    debit (referencing the SUBSCRIPTION), apply the renewal and commit —
    without creating an Order and without the old hard-refusal message."""
    sub = _make_sub(plan_id=None)
    rd = _make_rd(sub)
    mock_session.scalar = AsyncMock(return_value=sub)
    callback, loading_msg = _make_callback()
    *ctxs, wm_instance = _patches(rd)

    with ctxs[0], ctxs[1], ctxs[2] as apply_mock, ctxs[3], ctxs[4], ctxs[5] as ses, ctxs[6], ctxs[7]:
        await renew_pay_wallet(callback, _cb_data(sub), AsyncMock(), mock_session)

        wm_instance.process_transaction.assert_awaited_once()
        kwargs = wm_instance.process_transaction.await_args.kwargs
        assert kwargs["direction"] == "debit"
        assert kwargs["transaction_type"] == "renewal"
        assert kwargs["amount"] == Decimal("5.00")
        # No Order exists for a plan-less sub → ledger references the sub.
        assert kwargs["reference_type"] == "subscription"
        assert kwargs["reference_id"] == sub.id

        apply_mock.assert_awaited_once_with(sub, "volume", 10.0, mock_session)
        mock_session.commit.assert_awaited_once()
        # No Order row was created (Order.plan_id is NOT NULL).
        assert not any(
            isinstance(c.args[0], Order) for c in mock_session.add.call_args_list
        )
        # The old dead-end message is gone.
        for c in ses.await_args_list:
            assert "پلن این سرویس حذف شده" not in str(c)


@pytest.mark.asyncio
async def test_wallet_renewal_still_creates_order_for_planful_sub(mock_session):
    """Regression guard: subs WITH a plan keep the old behaviour — an Order is
    created and the debit references it."""
    sub = _make_sub(plan_id=uuid4())
    rd = _make_rd(sub)
    mock_session.scalar = AsyncMock(return_value=sub)
    callback, _ = _make_callback()
    *ctxs, wm_instance = _patches(rd)

    with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6], ctxs[7]:
        await renew_pay_wallet(callback, _cb_data(sub), AsyncMock(), mock_session)

        orders = [c.args[0] for c in mock_session.add.call_args_list if isinstance(c.args[0], Order)]
        assert len(orders) == 1
        assert orders[0].plan_id == sub.plan_id
        kwargs = wm_instance.process_transaction.await_args.kwargs
        assert kwargs["reference_type"] == "order"


@pytest.mark.asyncio
async def test_planless_panel_failure_refund_references_subscription(mock_session):
    """If apply_renewal fails for a plan-less sub, the refund credit must also
    reference the subscription, and the missing Order must not crash the
    refund path (order.status = 'failed' is guarded)."""
    sub = _make_sub(plan_id=None)
    rd = _make_rd(sub)
    mock_session.scalar = AsyncMock(return_value=sub)
    callback, loading_msg = _make_callback()
    failing_apply = AsyncMock(side_effect=RuntimeError("panel down"))
    *ctxs, wm_instance = _patches(rd, apply_renewal_mock=failing_apply)

    with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6], ctxs[7]:
        # Must NOT raise (no AttributeError on a None order).
        await renew_pay_wallet(callback, _cb_data(sub), AsyncMock(), mock_session)

        assert wm_instance.process_transaction.await_count == 2
        refund_kwargs = wm_instance.process_transaction.await_args_list[1].kwargs
        assert refund_kwargs["direction"] == "credit"
        assert refund_kwargs["transaction_type"] == "refund"
        assert refund_kwargs["reference_type"] == "subscription"
        assert refund_kwargs["reference_id"] == sub.id
        # The renewal was not committed.
        mock_session.commit.assert_not_awaited()
