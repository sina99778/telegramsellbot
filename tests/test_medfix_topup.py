"""Regression tests for the manual-crypto hash submission fix (finding 49):

* manual_hash_submitted loads the Payment row FOR UPDATE so it serializes
  against the crypto-autoconfirm worker (which credits under FOR UPDATE);
* a payment the worker already confirmed ("finished") must NOT be regressed
  back to "pending_approval" — the admin card would show an unverified-looking
  payment whose reject button marks an already-credited payment as rejected.
  The user is told it was auto-confirmed and NO admin card is sent;
* any other non-waiting status (e.g. "rejected") is likewise terminal;
* a payment still in "waiting_hash" proceeds exactly as before.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

MODULE = "apps.bot.handlers.user.topup"

TX_HASH = "a1b2c3d4e5f6a7b8c9d0e1f2"


def _make_message():
    msg = MagicMock()
    msg.text = TX_HASH
    msg.from_user = NS(id=999222)
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


def _make_state(payment_id):
    state = MagicMock()
    state.get_data = AsyncMock(return_value={"manual_payment_id": str(payment_id)})
    state.clear = AsyncMock()
    return state


def _make_manual_payment(payment_id, status):
    return NS(
        id=payment_id,
        payment_status=status,
        provider_payment_id=None,
        callback_payload={"manual": True, "currency": "TRX"},
        price_amount=Decimal("5.00"),
        pay_currency="TRX",
    )


@pytest.mark.asyncio
async def test_finished_payment_is_not_regressed(mock_session):
    from apps.bot.handlers.user.topup import manual_hash_submitted

    pid = uuid4()
    payment = _make_manual_payment(pid, "finished")
    mock_session.scalar = AsyncMock(return_value=payment)
    message = _make_message()

    await manual_hash_submitted(message, _make_state(pid), mock_session)

    # The credited payment must stay terminal — NOT flip to pending_approval.
    assert payment.payment_status == "finished"
    assert payment.provider_payment_id is None
    assert "tx_hash" not in payment.callback_payload
    # No admin approval card for an already-confirmed payment.
    message.bot.send_message.assert_not_awaited()
    # The user is told it was already auto-confirmed.
    text = message.answer.await_args.args[0]
    assert "قبلاً" in text and "تأیید" in text


@pytest.mark.asyncio
async def test_payment_loaded_with_row_lock(mock_session):
    from apps.bot.handlers.user.topup import manual_hash_submitted

    pid = uuid4()
    payment = _make_manual_payment(pid, "finished")
    mock_session.scalar = AsyncMock(return_value=payment)

    await manual_hash_submitted(_make_message(), _make_state(pid), mock_session)

    stmt = mock_session.scalar.await_args.args[0]
    assert stmt._for_update_arg is not None  # SELECT ... FOR UPDATE


@pytest.mark.asyncio
async def test_rejected_payment_is_terminal(mock_session):
    from apps.bot.handlers.user.topup import manual_hash_submitted

    pid = uuid4()
    payment = _make_manual_payment(pid, "rejected")
    mock_session.scalar = AsyncMock(return_value=payment)
    message = _make_message()

    await manual_hash_submitted(message, _make_state(pid), mock_session)

    assert payment.payment_status == "rejected"
    assert payment.provider_payment_id is None
    message.bot.send_message.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "پردازش شده" in text


@pytest.mark.asyncio
async def test_waiting_hash_proceeds_to_pending_approval(mock_session):
    from apps.bot.handlers.user.topup import manual_hash_submitted

    pid = uuid4()
    payment = _make_manual_payment(pid, "waiting_hash")
    mock_session.scalar = AsyncMock(return_value=payment)
    message = _make_message()

    repo = MagicMock()
    repo.get_by_telegram_id = AsyncMock(return_value=NS(first_name="Ali"))
    with patch(f"{MODULE}.UserRepository", return_value=repo):
        await manual_hash_submitted(message, _make_state(pid), mock_session)

    # The happy path is unchanged: hash recorded, status moves forward.
    assert payment.payment_status == "pending_approval"
    assert payment.provider_payment_id == TX_HASH
    assert payment.callback_payload.get("tx_hash") == TX_HASH
