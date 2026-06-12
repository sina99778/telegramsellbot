"""Regression tests for the free-trial double-claim race fix:

* the claim path is serialized by a Redis lock keyed free_trial_lock:{user.id};
* a lock miss answers "in progress" and does NOT provision;
* the has-received flag is re-checked INSIDE the lock (fresh read), so a
  concurrent handler that already claimed the trial blocks the second claim;
* on success the session is committed BEFORE the lock is released, closing
  the lock-released-before-commit window.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

MODULE = "apps.bot.handlers.user.free_trial"


def _make_message():
    msg = MagicMock()
    msg.from_user = NS(id=999111)
    msg.answer = AsyncMock()
    return msg


def _make_user():
    return NS(id=uuid4(), telegram_id=999111, has_received_free_trial=False)


def _make_plan():
    return NS(id=uuid4(), currency="USD")


def _fake_lock(acquired, record, session):
    """A distributed_lock stand-in that records the key/ttl and whether the
    session was already committed by the time the lock is released."""

    @asynccontextmanager
    async def _lock(key, ttl_seconds=30):
        record["key"] = key
        record["ttl"] = ttl_seconds
        try:
            yield acquired
        finally:
            record["committed_before_release"] = session.commit.await_count > 0

    return _lock


def _patches(user, plan, mock_session, lock):
    user_repo = MagicMock()
    user_repo.get_by_telegram_id = AsyncMock(return_value=user)
    settings_repo = MagicMock()
    settings_repo.get_trial_settings = AsyncMock(return_value=NS(enabled=True))
    pm = MagicMock()
    pm.provision_subscription = AsyncMock(
        return_value=NS(sub_link="https://sub.example/x", vless_uri="vless://x")
    )
    mock_session.scalar = AsyncMock(return_value=plan)  # plan lookup
    return (
        patch(f"{MODULE}.UserRepository", return_value=user_repo),
        patch(f"{MODULE}.AppSettingsRepository", return_value=settings_repo),
        patch(f"{MODULE}.get_verified_phone", return_value=None),
        patch(f"{MODULE}.ProvisioningManager", return_value=pm),
        patch(f"{MODULE}.distributed_lock", lock),
    ), pm


@pytest.mark.asyncio
async def test_lock_miss_does_not_provision(mock_session):
    from apps.bot.handlers.user.free_trial import free_trial_handler

    user, plan = _make_user(), _make_plan()
    record: dict = {}
    lock = _fake_lock(acquired=False, record=record, session=mock_session)
    (p1, p2, p3, p4, p5), pm = _patches(user, plan, mock_session, lock)
    message = _make_message()

    with p1, p2, p3, p4, p5:
        await free_trial_handler(message, mock_session, MagicMock())

    pm.provision_subscription.assert_not_awaited()
    mock_session.add.assert_not_called()                # no Order row created
    assert user.has_received_free_trial is False
    mock_session.commit.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "در حال پردازش" in text


@pytest.mark.asyncio
async def test_flag_recheck_inside_lock_blocks_second_claim(mock_session):
    from apps.bot.handlers.user.free_trial import free_trial_handler

    user, plan = _make_user(), _make_plan()
    record: dict = {}
    lock = _fake_lock(acquired=True, record=record, session=mock_session)

    async def _refresh(obj, attribute_names=None):
        # Simulate a concurrent handler having claimed + committed the trial.
        obj.has_received_free_trial = True

    mock_session.refresh = AsyncMock(side_effect=_refresh)
    (p1, p2, p3, p4, p5), pm = _patches(user, plan, mock_session, lock)
    message = _make_message()

    with p1, p2, p3, p4, p5:
        await free_trial_handler(message, mock_session, MagicMock())

    mock_session.refresh.assert_awaited_once()          # fresh read, not stale ORM state
    pm.provision_subscription.assert_not_awaited()
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "قبلاً" in text


@pytest.mark.asyncio
async def test_happy_path_locks_provisions_and_commits_before_release(mock_session):
    from apps.bot.handlers.user.free_trial import free_trial_handler

    user, plan = _make_user(), _make_plan()
    record: dict = {}
    lock = _fake_lock(acquired=True, record=record, session=mock_session)
    (p1, p2, p3, p4, p5), pm = _patches(user, plan, mock_session, lock)
    message = _make_message()

    with p1, p2, p3, p4, p5:
        await free_trial_handler(message, mock_session, MagicMock())

    assert record["key"] == f"free_trial_lock:{user.id}"
    pm.provision_subscription.assert_awaited_once()
    assert user.has_received_free_trial is True
    # The claim must be durable BEFORE the lock is released.
    assert record["committed_before_release"] is True
    text = message.answer.await_args.args[0]
    assert "کانفیگ تست شما آماده است" in text


@pytest.mark.asyncio
async def test_already_claimed_fast_path_skips_lock(mock_session):
    from apps.bot.handlers.user.free_trial import free_trial_handler

    user, plan = _make_user(), _make_plan()
    user.has_received_free_trial = True
    record: dict = {}
    lock = _fake_lock(acquired=True, record=record, session=mock_session)
    (p1, p2, p3, p4, p5), pm = _patches(user, plan, mock_session, lock)
    message = _make_message()

    with p1, p2, p3, p4, p5:
        await free_trial_handler(message, mock_session, MagicMock())

    assert record == {}                                 # lock never entered
    pm.provision_subscription.assert_not_awaited()
    text = message.answer.await_args.args[0]
    assert "قبلاً" in text
