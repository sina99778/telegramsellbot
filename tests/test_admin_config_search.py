"""Tests for the admin GLOBAL config search (services/admin_transfer.py).

The search itself runs Postgres-specific SQL, so we test:
  * the pure helpers (id detection, labels, filter construction), and
  * the search_configs control flow against a mock session (short-circuit on
    zero results; row path returns what the query yields) — verifying the
    statement builds without error and the count/rows wiring is correct.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.admin_transfer import (
    _config_search_filter,
    _looks_like_telegram_id,
    config_label,
    config_search_label,
    owner_label,
    search_configs,
    status_fa,
)


# ─── _looks_like_telegram_id ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456789", 123456789),
        ("@123456789", 123456789),  # @-prefixed numeric id
        ("  555000  ", 555000),
        ("hello", None),
        ("ali99", None),
        ("@username", None),
        ("12", None),  # too short to be a real id
        ("1" * 20, None),  # absurdly long
        ("", None),
    ],
)
def test_looks_like_telegram_id(raw, expected):
    assert _looks_like_telegram_id(raw) == expected


# ─── owner_label ──────────────────────────────────────────────────────────────


def test_owner_label_prefers_username():
    u = SimpleNamespace(username="ali", first_name="Ali", telegram_id=42)
    assert owner_label(u) == "@ali"


def test_owner_label_falls_back_to_name_then_id():
    u = SimpleNamespace(username=None, first_name="Sara", telegram_id=42)
    assert owner_label(u) == "Sara (42)"
    u2 = SimpleNamespace(username=None, first_name=None, telegram_id=42)
    assert owner_label(u2) == "42"


def test_owner_label_handles_none():
    assert owner_label(None) == "بدون مالک"


# ─── labels ───────────────────────────────────────────────────────────────────


def _fake_sub(*, username="cfg-ali", status="active", owner_username="ali"):
    return SimpleNamespace(
        id=uuid4(),
        status=status,
        xui_client=SimpleNamespace(username=username, email="ali@x", sub_link="https://s/abc"),
        plan=SimpleNamespace(name="Pro 50G"),
        user=SimpleNamespace(username=owner_username, first_name="Ali", telegram_id=12345),
    )


def test_config_label_uses_panel_name_and_status():
    sub = _fake_sub(username="cfg-ali", status="active")
    label = config_label(sub)
    assert "cfg-ali" in label
    assert status_fa("active") in label  # "فعال"


def test_config_search_label_appends_owner():
    sub = _fake_sub(username="cfg-ali", owner_username="ali")
    label = config_search_label(sub)
    assert "cfg-ali" in label
    assert "@ali" in label  # owner shown
    assert "👤" in label


# ─── _config_search_filter ────────────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "*", "همه", "all"])
def test_filter_is_none_for_show_all_tokens(blank):
    assert _config_search_filter(blank) is None


def test_filter_for_text_query_is_built():
    expr = _config_search_filter("vpn")
    assert expr is not None
    # A plain text query must NOT add a telegram_id equality.
    assert "telegram_id" not in str(expr)


def test_filter_for_numeric_query_adds_telegram_id():
    expr = _config_search_filter("123456789")
    assert expr is not None
    # A bare numeric query should additionally match the owner's telegram_id.
    assert "telegram_id" in str(expr)


# ─── search_configs control flow ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_short_circuits_on_zero_total():
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    session.execute = AsyncMock()

    rows, total = await search_configs(session, "nothing-matches", limit=8, offset=0)

    assert (rows, total) == ([], 0)
    # No point running the rows query when the count is zero.
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_returns_rows_when_count_positive():
    fake_rows = [_fake_sub(), _fake_sub(username="cfg-two")]

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=2)
    exec_result = MagicMock()
    scalars = MagicMock()
    scalars.unique.return_value.all.return_value = fake_rows
    exec_result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=exec_result)

    rows, total = await search_configs(session, "cfg", limit=8, offset=0)

    assert total == 2
    assert rows == fake_rows
    session.execute.assert_awaited_once()
