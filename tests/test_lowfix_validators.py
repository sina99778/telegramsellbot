"""Regression tests for finding 76: 'nan'/'inf' passing amount validators.

float('nan') <= 0 and float('nan') < min are both False, so 'nan' used to sail
through every "amount <= 0" style validator and crash the money flow later
(decimal.InvalidOperation on wallet comparison, OverflowError on int(inf),
ValueError on int(nan * 1024**3)). Decimal('NaN') is worse: the <= comparison
itself raises InvalidOperation OUTSIDE the parse try/except, and
Decimal('Infinity') passed entirely, producing Payment rows with
price_amount=Infinity.

Fixed validators:
* renewal.renew_value_entered      — math.isfinite + upper bound (100k)
* renewal._get_renewal_data        — same gate on forged callback payloads
* topup.topup_custom_amount_handler — Decimal.is_finite + upper bound (100k)
* topup.topup_preset_handler        — forged callback data hardened
* purchase.custom_purchase_volume_entered — math.isfinite + upper bound
* purchase.custom_purchase_days_entered   — upper bound (datetime overflow)
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.texts import Messages


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_message(text):
    msg = MagicMock()
    msg.text = text
    msg.from_user = NS(id=424242)
    msg.answer = AsyncMock()
    return msg


def _make_state(data=None):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data or {})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    return state


def _make_callback(data):
    cb = MagicMock()
    cb.data = data
    cb.from_user = NS(id=424242)
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------------------
# renewal.renew_value_entered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "1e300", "999999999"])
@pytest.mark.asyncio
async def test_renew_value_rejects_nonfinite_and_huge(mock_session, raw):
    from apps.bot.handlers.user.renewal import renew_value_entered

    message = _make_message(raw)
    state = _make_state({"sub_id": uuid4().hex, "renew_type": "volume"})

    await renew_value_entered(message, state, mock_session)

    message.answer.assert_awaited_once_with(Messages.RENEWAL_INVALID_VALUE)
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_renew_value_time_nan_does_not_reach_int_cast(mock_session):
    """'nan' on the TIME path used to blow up at int(amount) — now it is
    rejected at parse time with the standard invalid-value message."""
    from apps.bot.handlers.user.renewal import renew_value_entered

    message = _make_message("nan")
    state = _make_state({"sub_id": uuid4().hex, "renew_type": "time"})

    await renew_value_entered(message, state, mock_session)

    message.answer.assert_awaited_once_with(Messages.RENEWAL_INVALID_VALUE)
    state.set_state.assert_not_awaited()


# ---------------------------------------------------------------------------
# renewal._get_renewal_data (forged callback payloads)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e300", "200000", "-5", "abc"])
@pytest.mark.asyncio
async def test_get_renewal_data_rejects_bad_amounts(mock_session, raw):
    from apps.bot.handlers.user.renewal import _get_renewal_data

    cb_data = NS(m="w", s=uuid4().hex, t="v", a=raw)
    with patch("apps.bot.handlers.user.renewal.UserRepository") as repo_cls:
        rd = await _get_renewal_data(cb_data, mock_session, 424242)

    assert rd is None
    # Rejected before any DB access.
    repo_cls.assert_not_called()


@pytest.mark.asyncio
async def test_get_renewal_data_finite_amount_passes_validator(mock_session):
    """Control: a normal amount gets past the gate (user lookup is reached)."""
    from apps.bot.handlers.user.renewal import _get_renewal_data

    cb_data = NS(m="w", s=uuid4().hex, t="v", a="10")
    with patch("apps.bot.handlers.user.renewal.UserRepository") as repo_cls:
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=None)
        rd = await _get_renewal_data(cb_data, mock_session, 424242)

    assert rd is None  # no user in mock DB — but the validator let it through
    repo_cls.return_value.get_by_telegram_id.assert_awaited_once()


# ---------------------------------------------------------------------------
# topup.topup_custom_amount_handler
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf"])
@pytest.mark.asyncio
async def test_topup_custom_rejects_nonfinite(mock_session, raw):
    """Decimal('NaN') <= 0 raises InvalidOperation — the handler must reject
    non-finite input with the invalid-amount message instead of crashing."""
    from apps.bot.handlers.user.topup import topup_custom_amount_handler

    message = _make_message(raw)
    state = _make_state()

    await topup_custom_amount_handler(message, state, mock_session)

    message.answer.assert_awaited_once_with(Messages.TOPUP_INVALID_AMOUNT)
    state.update_data.assert_not_awaited()


@pytest.mark.parametrize("raw", ["100001", "1E+1000000"])
@pytest.mark.asyncio
async def test_topup_custom_rejects_over_cap(mock_session, raw):
    from apps.bot.handlers.user.topup import topup_custom_amount_handler

    message = _make_message(raw)
    state = _make_state()

    await topup_custom_amount_handler(message, state, mock_session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "حداکثر" in text
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_custom_zero_keeps_gt_zero_message(mock_session):
    from apps.bot.handlers.user.topup import topup_custom_amount_handler

    message = _make_message("0")
    state = _make_state()

    await topup_custom_amount_handler(message, state, mock_session)

    message.answer.assert_awaited_once_with(Messages.TOPUP_AMOUNT_GT_ZERO)
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_custom_valid_amount_proceeds(mock_session):
    from apps.bot.handlers.user.topup import topup_custom_amount_handler

    message = _make_message("12.50")
    state = _make_state()

    gw = NS(
        nowpayments_enabled=False,
        tetrapay_enabled=False,
        manual_crypto_enabled=False,
        manual_crypto_wallets=None,
        manual_crypto_address=None,
        card_to_card_enabled=False,
        cards=None,
        card_number=None,
    )
    with patch("repositories.settings.AppSettingsRepository") as repo_cls, \
         patch("apps.bot.keyboards.inline.build_gateway_selection_keyboard", return_value=None):
        repo_cls.return_value.get_gateway_settings = AsyncMock(return_value=gw)
        await topup_custom_amount_handler(message, state, mock_session)

    state.update_data.assert_awaited_once_with(topup_amount="12.50")


# ---------------------------------------------------------------------------
# topup.topup_preset_handler (forged callback data)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "abc", "-5", "0", "100001"])
@pytest.mark.asyncio
async def test_topup_preset_rejects_forged_amounts(mock_session, raw):
    from apps.bot.handlers.user.topup import topup_preset_handler

    callback = _make_callback(f"wallet:topup:preset:{raw}")
    state = _make_state()

    with patch("apps.bot.handlers.user.topup.safe_edit_or_send", new=AsyncMock()) as send:
        await topup_preset_handler(callback, state, mock_session)

    send.assert_awaited_once_with(callback, Messages.TOPUP_INVALID_AMOUNT)
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_preset_valid_amount_proceeds(mock_session):
    from apps.bot.handlers.user.topup import topup_preset_handler

    callback = _make_callback("wallet:topup:preset:25")
    state = _make_state()

    gw = NS(
        nowpayments_enabled=False,
        tetrapay_enabled=False,
        tronado_enabled=False,
        manual_crypto_enabled=False,
        manual_crypto_wallets=None,
        manual_crypto_address=None,
        card_to_card_enabled=False,
        cards=None,
        card_number=None,
    )
    with patch("apps.bot.handlers.user.topup.safe_edit_or_send", new=AsyncMock()), \
         patch("repositories.settings.AppSettingsRepository") as repo_cls, \
         patch("apps.bot.keyboards.inline.build_gateway_selection_keyboard", return_value=None):
        repo_cls.return_value.get_gateway_settings = AsyncMock(return_value=gw)
        await topup_preset_handler(callback, state, mock_session)

    state.update_data.assert_awaited_once_with(topup_amount="25")


# ---------------------------------------------------------------------------
# purchase.custom_purchase_volume_entered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e300", "100001"])
@pytest.mark.asyncio
async def test_custom_volume_rejects_nonfinite_and_huge(raw):
    from apps.bot.handlers.user.purchase import custom_purchase_volume_entered

    message = _make_message(raw)
    state = _make_state()

    await custom_purchase_volume_entered(message, state)

    message.answer.assert_awaited_once()
    assert "حجم معتبر نیست" in message.answer.await_args.args[0]
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_volume_valid_proceeds():
    from apps.bot.handlers.user.purchase import custom_purchase_volume_entered

    message = _make_message("25")
    state = _make_state()

    await custom_purchase_volume_entered(message, state)

    state.update_data.assert_awaited_once_with(custom_volume_gb=25.0)
    state.set_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# purchase.custom_purchase_days_entered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_days_rejects_astronomical_count(mock_session):
    """A giant day count overflows datetime arithmetic downstream — it must be
    rejected at parse time."""
    from apps.bot.handlers.user.purchase import custom_purchase_days_entered

    message = _make_message("999999999999")
    state = _make_state()

    await custom_purchase_days_entered(message, state, mock_session)

    message.answer.assert_awaited_once()
    assert "مدت معتبر نیست" in message.answer.await_args.args[0]
    # Rejected before the FSM data / settings are even read.
    state.get_data.assert_not_awaited()
