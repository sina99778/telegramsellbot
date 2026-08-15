"""Regression tests for the high-severity money-path fixes:

* process_successful_payment writes the credited markers only AFTER a
  successful wallet credit (a failed credit must stay retriable, not be
  committed as credited).
* _handle_direct_renewal refuses to renew a disabled sub (old payment buttons
  must not resurrect punitively-disabled configs) and requires the canonical
  renewal lock.
* _get_renewal_data enforces ownership + status + renewability for ALL
  gateway renewal buttons.
* volume_renewal_blocked: volume cannot resurrect a time-expired config.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ─── credited markers only after a successful credit ─────────────────────────


@pytest.mark.asyncio
async def test_failed_credit_leaves_payment_retriable(mock_session, make_payment):
    from services.payment import process_successful_payment

    payment = make_payment(kind="wallet_topup", payment_status="confirmed", actually_paid=None)

    wm = MagicMock()
    wm.process_transaction = AsyncMock(side_effect=ValueError("credit failed"))
    with patch("services.payment.WalletManager", return_value=wm):
        with pytest.raises(ValueError):
            await process_successful_payment(mock_session, payment, Decimal("5.00"))

    # The half-state that used to be committed must no longer exist:
    assert payment.actually_paid is None
    assert payment.payment_status == "confirmed"   # NOT "finished"


@pytest.mark.asyncio
async def test_successful_credit_sets_markers(mock_session, make_payment):
    from services.payment import process_successful_payment

    payment = make_payment(kind="wallet_topup", payment_status="confirmed", actually_paid=None)

    wm = MagicMock()
    wm.process_transaction = AsyncMock()
    with patch("services.payment.WalletManager", return_value=wm):
        await process_successful_payment(mock_session, payment, Decimal("5.00"))

    wm.process_transaction.assert_awaited_once()
    assert payment.actually_paid == Decimal("5.00")
    assert payment.payment_status == "finished"


# ─── IPN renewal: status gate + canonical lock ────────────────────────────────


def _renewal_payment(make_payment, sub_id):
    return make_payment(
        kind="direct_renewal",
        callback_payload={"sub_id": str(sub_id), "renew_type": "volume", "renew_amount": 10},
    )


@pytest.mark.asyncio
async def test_ipn_renewal_refuses_disabled_sub(mock_session, make_payment):
    from services.payment import _handle_direct_renewal

    sub = NS(id=uuid4(), status="disabled", xui_client=None)
    payment = _renewal_payment(make_payment, sub.id)
    mock_session.scalar = AsyncMock(side_effect=[sub, None])  # sub, then user lookup

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    @asynccontextmanager
    async def fake_lock(key, ttl_seconds=60):
        yield True

    with patch("core.redis.distributed_lock", fake_lock), \
         patch("services.payment._get_shared_bot", return_value=bot), \
         patch("services.payment.WalletManager") as wm:
        result = await _handle_direct_renewal(mock_session, payment)

    assert result is False
    wm.assert_not_called()                                  # no debit, money stays in wallet
    assert payment.callback_payload.get("renewal_refused") is True


@pytest.mark.asyncio
async def test_renewal_lock_is_held_before_credit_through_apply(mock_session, make_payment):
    from services.payment import process_successful_payment

    sub_id = uuid4()
    payment = _renewal_payment(make_payment, sub_id)
    events = []
    locked = False

    lock_ttls = []

    @asynccontextmanager
    async def fake_lock(key, ttl_seconds=60):
        nonlocal locked
        lock_ttls.append(ttl_seconds)
        events.append("lock")
        locked = True
        yield True
        locked = False
        events.append("unlock")

    async def wallet_transaction(**kwargs):
        assert locked is True
        events.append(kwargs["direction"])

    async def handle(session, current_payment, *, lock_held=False):
        assert locked is True
        assert lock_held is True
        events.append("renew")
        return True

    wallet_manager = MagicMock()
    wallet_manager.process_transaction = AsyncMock(side_effect=wallet_transaction)

    with patch("core.redis.distributed_lock", fake_lock), \
         patch("services.payment.WalletManager", return_value=wallet_manager), \
         patch("services.payment._handle_direct_renewal", new=handle):
        result = await process_successful_payment(mock_session, payment, Decimal("5.00"))

    assert result is True
    assert events == ["lock", "credit", "renew", "unlock"]
    assert lock_ttls == [120]
    assert payment.actually_paid == Decimal("5.00")


@pytest.mark.asyncio
async def test_renewal_lock_miss_preserves_credit_retry(mock_session, make_payment):
    from services.payment import process_successful_payment

    payment = _renewal_payment(make_payment, uuid4())

    @asynccontextmanager
    async def fake_lock(key, ttl_seconds=60):
        yield False

    wallet_manager = MagicMock()
    wallet_manager.process_transaction = AsyncMock()

    with patch("core.redis.distributed_lock", fake_lock), \
         patch("services.payment.WalletManager", return_value=wallet_manager):
        result = await process_successful_payment(mock_session, payment, Decimal("5.00"))

    assert result is False
    assert payment.actually_paid is None
    wallet_manager.process_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_ipn_renewal_lock_miss_defers(mock_session, make_payment):
    from services.payment import _handle_direct_renewal

    sub = NS(id=uuid4(), status="active", xui_client=None)
    payment = _renewal_payment(make_payment, sub.id)
    mock_session.scalar = AsyncMock(return_value=sub)

    @asynccontextmanager
    async def fake_lock(key, ttl_seconds=60):
        yield False  # lock NOT acquired

    with patch("core.redis.distributed_lock", fake_lock), \
         patch("services.payment.WalletManager") as wm:
        result = await _handle_direct_renewal(mock_session, payment)

    assert result is False                                  # deferred to IPN retry
    wm.assert_not_called()
    assert "renewal_refused" not in (payment.callback_payload or {})


@pytest.mark.asyncio
async def test_renewal_refused_is_terminal_in_processor(mock_session, make_payment):
    from services.payment import process_successful_payment

    payment = make_payment(
        kind="direct_renewal",
        payment_status="finished",
        actually_paid=Decimal("5.00"),
        callback_payload={"renewal_refused": True},
    )
    with patch("services.payment._handle_direct_renewal", AsyncMock()) as hdr:
        result = await process_successful_payment(mock_session, payment, Decimal("5.00"))
    assert result is False
    hdr.assert_not_awaited()


# ─── gateway renewal buttons all inherit the ownership/status gate ───────────


def _cb(sub_id):
    from apps.bot.handlers.user.renewal import RenewPayCallback
    return RenewPayCallback(m="n", s=sub_id.hex, t="v", a="10")


@pytest.mark.asyncio
async def test_get_renewal_data_rejects_foreign_or_missing_sub(mock_session):
    import apps.bot.handlers.user.renewal as rmod

    user = NS(id=uuid4(), personal_discount_percent=0)
    repo = MagicMock()
    repo.get_by_telegram_id = AsyncMock(return_value=user)
    mock_session.scalar = AsyncMock(return_value=None)      # sub not found / not owned

    with patch.object(rmod, "UserRepository", return_value=repo):
        rd = await rmod._get_renewal_data(_cb(uuid4()), mock_session, 123)
    assert rd is None


@pytest.mark.asyncio
async def test_get_renewal_data_rejects_disabled_sub(mock_session):
    import apps.bot.handlers.user.renewal as rmod

    user = NS(id=uuid4(), personal_discount_percent=0)
    repo = MagicMock()
    repo.get_by_telegram_id = AsyncMock(return_value=user)
    sub = NS(id=uuid4(), status="disabled", ends_at=None)
    mock_session.scalar = AsyncMock(return_value=sub)

    with patch.object(rmod, "UserRepository", return_value=repo):
        rd = await rmod._get_renewal_data(_cb(sub.id), mock_session, 123)
    assert rd is None


# ─── volume cannot resurrect a time-expired config ───────────────────────────


def test_volume_renewal_blocked_matrix():
    from services.renewal import volume_renewal_blocked

    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    assert volume_renewal_blocked(NS(ends_at=past), "volume") is True
    assert volume_renewal_blocked(NS(ends_at=future), "volume") is False
    assert volume_renewal_blocked(NS(ends_at=None), "volume") is False   # not yet timed
    assert volume_renewal_blocked(NS(ends_at=past), "time") is False     # time renewals fine


@pytest.mark.asyncio
async def test_apply_renewal_raises_for_volume_on_time_expired(mock_session):
    from services.renewal import RenewalNotAllowedError, apply_renewal

    sub = NS(
        id=uuid4(), plan_id=None, status="expired",
        volume_bytes=10 * 1024**3, used_bytes=0, lifetime_used_bytes=0,
        ends_at=datetime.now(timezone.utc) - timedelta(days=2), activated_at=None,
    )
    with pytest.raises(RenewalNotAllowedError):
        await apply_renewal(session=mock_session, subscription=sub, renew_type="volume", amount=5)
    assert sub.volume_bytes == 10 * 1024**3                # untouched
