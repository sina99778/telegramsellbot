"""Regression tests for the low-severity auto-renew fix (finding #77):

When the wallet debit races and raises InsufficientBalanceError, _try_auto_renew
rolls back the session — which expires every loaded ORM instance. The
insufficient-balance notifier must therefore work from PRIMITIVE snapshots taken
before the rollback (telegram_id, sub id/name, blocked flag), not from the
now-expired ORM objects, or the first attribute access raises MissingGreenlet
and the user is never told their wallet was too low.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.app_setting import AppSetting
from services.wallet.manager import InsufficientBalanceError


def _now():
    return datetime.now(timezone.utc)


class _Expirable:
    """Stand-in for an ORM instance: any attribute access after expire() raises,
    mimicking the MissingGreenlet blow-up of an expired instance on AsyncSession."""

    def __init__(self, **attrs):
        self.__dict__["_attrs"] = dict(attrs)
        self.__dict__["_expired"] = False

    def expire(self):
        self.__dict__["_expired"] = True

    def __getattr__(self, name):
        if self.__dict__["_expired"]:
            raise RuntimeError(f"MissingGreenlet: refresh of {name!r} on expired instance")
        try:
            return self.__dict__["_attrs"][name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        if self.__dict__["_expired"]:
            raise RuntimeError(f"MissingGreenlet: write to {name!r} on expired instance")
        self.__dict__["_attrs"][name] = value


def _make_sub(balance=Decimal("100.00"), is_blocked=False):
    user = _Expirable(
        id=uuid4(),
        wallet=NS(balance=balance),
        is_bot_blocked=is_blocked,
        telegram_id=4242,
    )
    sub = _Expirable(
        id=uuid4(),
        plan_id=uuid4(),
        user=user,
        plan=NS(duration_days=30),
        xui_client=NS(username="cfg-user"),
    )
    return sub, user


@asynccontextmanager
async def _acquired_lock(key, ttl_seconds=60):
    yield True


async def _run(mock_session, bot, sub, user, *, debit_raises=True):
    """Drive _try_auto_renew with the lock acquired, an eligible fresh row, and
    a rollback that expires the ORM stand-ins (like the real Session does)."""
    from apps.worker.jobs.auto_renew import _try_auto_renew

    fresh = NS(auto_renew_enabled=True, status="active", ends_at=_now() + timedelta(hours=1))
    fresh_result = MagicMock()
    fresh_result.one_or_none.return_value = fresh
    mock_session.execute = AsyncMock(return_value=fresh_result)

    def _expire_all():
        sub.expire()
        user.expire()

    mock_session.rollback = AsyncMock(side_effect=_expire_all)

    wm = MagicMock()
    wm.process_transaction = AsyncMock(
        side_effect=InsufficientBalanceError("balance dropped") if debit_raises else None
    )
    apply = AsyncMock()
    with patch("apps.worker.jobs.auto_renew.distributed_lock", _acquired_lock), \
         patch("apps.worker.jobs.auto_renew.calculate_renewal_price", return_value=Decimal("5.00")), \
         patch("apps.worker.jobs.auto_renew.WalletManager", return_value=wm), \
         patch("apps.worker.jobs.auto_renew.apply_renewal", apply):
        await _try_auto_renew(mock_session, bot, sub, MagicMock())
    return wm, apply


def _added_appsettings(mock_session):
    return [c.args[0] for c in mock_session.add.call_args_list if isinstance(c.args[0], AppSetting)]


# ─── the core regression: notify survives the rollback that expires the ORM ──


@pytest.mark.asyncio
async def test_debit_race_still_notifies_user_after_rollback(mock_session):
    sub, user = _make_sub()
    sub_id, telegram_id = sub.id, user.telegram_id
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _run(mock_session, bot, sub, user)

    mock_session.rollback.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.call_args
    assert args[0] == telegram_id                     # snapshotted, not user.telegram_id
    assert "تمدید خودکار ناموفق بود" in args[1]
    assert "cfg-user" in args[1]                      # snapshotted sub name
    assert "5.00$" in args[1]
    # Dedup key written for the right subscription despite the expired sub object.
    keys = [s.key for s in _added_appsettings(mock_session)]
    assert f"alert.sub.{sub_id}.autorenew_low" in keys
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_dedup_key_suppresses_repeat_notification(mock_session):
    sub, user = _make_sub()
    mock_session.get = AsyncMock(return_value=object())  # dedup key already present
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _run(mock_session, bot, sub, user)

    bot.send_message.assert_not_awaited()
    assert _added_appsettings(mock_session) == []


@pytest.mark.asyncio
async def test_blocked_user_is_not_messaged(mock_session):
    sub, user = _make_sub(is_blocked=True)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _run(mock_session, bot, sub, user)

    bot.send_message.assert_not_awaited()
    assert _added_appsettings(mock_session) == []     # no dedup key burned either


@pytest.mark.asyncio
async def test_forbidden_persists_blocked_flag_via_plain_update(mock_session):
    from aiogram.exceptions import TelegramForbiddenError
    from sqlalchemy.sql.dml import Update

    sub, user = _make_sub()
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message="bot was blocked by the user")
    )

    await _run(mock_session, bot, sub, user)

    # The flag must be written with a SQL UPDATE (the ORM instance is expired).
    updates = [c.args[0] for c in mock_session.execute.call_args_list if isinstance(c.args[0], Update)]
    assert len(updates) == 1
    assert updates[0].table.name == "users"


@pytest.mark.asyncio
async def test_pre_check_low_balance_notifies_without_debit(mock_session):
    sub, user = _make_sub(balance=Decimal("1.00"))    # below the 5.00 price
    telegram_id = user.telegram_id
    bot = MagicMock()
    bot.send_message = AsyncMock()

    wm, apply = await _run(mock_session, bot, sub, user, debit_raises=False)

    wm.process_transaction.assert_not_awaited()       # never reached the debit
    apply.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.args[0] == telegram_id
