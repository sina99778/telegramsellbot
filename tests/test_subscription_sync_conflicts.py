"""Usage conflicts must retry fresh state without disabling version checks."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm.exc import StaleDataError

from apps.worker.jobs import subscriptions


async def test_concurrent_update_retries_the_full_read_in_a_new_attempt(caplog):
    run = AsyncMock(side_effect=[StaleDataError("fixture conflict"), None])
    with (
        patch.object(subscriptions, "_sync_all_subscription_states_once", run),
        caplog.at_level(logging.INFO, logger=subscriptions.__name__),
    ):
        await subscriptions.sync_all_subscription_states()
    assert run.await_count == 2
    assert "refreshing usage snapshot" in caplog.text
    assert "deferred" not in caplog.text


async def test_repeated_conflicts_are_bounded_and_deferred(caplog):
    run = AsyncMock(side_effect=StaleDataError("fixture conflict"))
    with patch.object(subscriptions, "_sync_all_subscription_states_once", run):
        await subscriptions.sync_all_subscription_states()
    assert run.await_count == 2
    assert "deferred after repeated concurrent updates" in caplog.text


async def test_unrelated_errors_are_not_hidden_as_concurrent_updates():
    run = AsyncMock(side_effect=ValueError("fixture unexpected error"))
    with (
        patch.object(subscriptions, "_sync_all_subscription_states_once", run),
        pytest.raises(ValueError, match="fixture unexpected error"),
    ):
        await subscriptions.sync_all_subscription_states()
    run.assert_awaited_once()
