"""
Regression tests for low-severity fix:

73. apps/bot/handlers/admin/plans.py: _normalize_integer_input silently
    DELETED every character in DECIMAL_SEPARATORS ('.', ',', '٫', '٬', '،'),
    so an admin typing '1.5' for a plan duration got a 15-day plan and '2.5'
    for volume got 25 GB — wrong by 10x, with a success message, straight
    into a sellable Plan row. Integer fields must now reject any input
    containing a decimal/thousands separator with ValueError, which every
    call site already catches and answers with the Persian
    AdminMessages.INVALID_INTEGER (or the inline ip-limit message).
    Genuinely decimal fields (prices) already go through
    _normalize_decimal_input + Decimal and are unaffected.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.bot.handlers.admin.plans import (
    DECIMAL_SEPARATORS,
    _normalize_decimal_input,
    _normalize_integer_input,
    create_plan_duration,
    edit_plan_duration_submit,
)
from core.texts import AdminMessages


# ─── _normalize_integer_input: separators must be rejected, not deleted ──────


class TestIntegerInputRejectsSeparators:
    @pytest.mark.parametrize(
        "raw",
        [
            "1.5",        # the reported repro: used to become '15'
            "2,5",        # decimal comma
            "1٫5",   # Arabic decimal separator ٫
            "1٬000", # Arabic thousands separator ٬ — used to become '1000'
            "1،5",   # Arabic comma ،
            "۱٫۵",        # Persian digits + Persian decimal separator
            "1.",         # trailing dot
            "1.0",        # zero fraction is still not an integer typo we accept
            "1,000",      # ambiguous thousands/decimal — rejected outright
        ],
    )
    def test_separator_input_raises_value_error(self, raw):
        with pytest.raises(ValueError):
            int(_normalize_integer_input(raw))

    def test_every_known_separator_is_covered(self):
        """Each member of DECIMAL_SEPARATORS individually triggers rejection."""
        for sep in DECIMAL_SEPARATORS:
            with pytest.raises(ValueError):
                _normalize_integer_input(f"1{sep}5")

    def test_one_point_five_never_becomes_fifteen(self):
        """The exact silent-corruption from the report must be impossible."""
        try:
            value = int(_normalize_integer_input("1.5"))
        except ValueError:
            return  # correct: rejected
        pytest.fail(f"'1.5' was silently accepted as {value}")


class TestIntegerInputStillAcceptsIntegers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("15", 15),
            ("  30  ", 30),          # surrounding whitespace
            ("1 000", 1000),         # internal spaces still tolerated
            ("۱۵", 15),              # Persian digits
            ("٢٠", 20),              # Arabic-Indic digits
            ("‎42‏", 42),  # bidi control marks stripped
            ("-3", -3),              # sign passes through; callers gate on >0/>=0
        ],
    )
    def test_valid_integers_unchanged(self, raw, expected):
        assert int(_normalize_integer_input(raw)) == expected

    def test_garbage_still_fails_at_int(self):
        with pytest.raises(ValueError):
            int(_normalize_integer_input("abc"))


# ─── _normalize_decimal_input: price fields keep accepting decimals ──────────


class TestDecimalInputUnaffected:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.5", Decimal("1.5")),
            ("1,5", Decimal("1.5")),
            ("۱٫۵", Decimal("1.5")),
            ("10", Decimal("10")),
        ],
    )
    def test_decimal_fields_parse(self, raw, expected):
        assert Decimal(_normalize_decimal_input(raw)) == expected


# ─── Handler-level: fractional input answers the Persian error ───────────────


def _make_message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.answer = AsyncMock()
    return message


def _make_state() -> AsyncMock:
    return AsyncMock()


class TestCreatePlanDurationHandler:
    async def test_fractional_duration_rejected_with_persian_error(self):
        message = _make_message("1.5")
        state = _make_state()

        await create_plan_duration(message, state)

        message.answer.assert_awaited_once_with(AdminMessages.INVALID_INTEGER)
        state.update_data.assert_not_awaited()
        state.set_state.assert_not_awaited()

    async def test_integer_duration_still_advances_flow(self):
        message = _make_message("30")
        state = _make_state()

        await create_plan_duration(message, state)

        state.update_data.assert_awaited_once_with(duration_days=30)
        state.set_state.assert_awaited_once()
        message.answer.assert_awaited_once_with(AdminMessages.ENTER_VOLUME)


class TestEditPlanDurationHandler:
    async def test_fractional_edit_rejected_before_touching_plan(self):
        message = _make_message("2,5")
        state = _make_state()
        session = MagicMock()
        session.get = AsyncMock()

        await edit_plan_duration_submit(message, state, session, MagicMock())

        message.answer.assert_awaited_once_with(AdminMessages.INVALID_INTEGER)
        state.clear.assert_not_awaited()
        session.get.assert_not_awaited()
