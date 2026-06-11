"""Regression test for the money-safety bug: GlobalErrorMiddleware runs INSIDE
DatabaseSessionMiddleware, so when it swallows a handler exception and returns
None the outer DB middleware would COMMIT the partial transaction (e.g. a wallet
debit with no config delivered). The fix: the error middleware must roll the
request session back before swallowing, so the outer commit is a no-op."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from apps.bot.middlewares.error_handler import GlobalErrorMiddleware


@pytest.mark.asyncio
async def test_unhandled_exception_rolls_back_the_session():
    mw = GlobalErrorMiddleware()
    session = AsyncMock()

    async def handler(event, data):
        raise ValueError("boom after a wallet debit")

    result = await mw(handler, object(), {"session": session})

    assert result is None                 # exception swallowed (user got a message)
    session.rollback.assert_awaited_once()  # ...but the partial txn was discarded
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_handler_does_not_roll_back():
    mw = GlobalErrorMiddleware()
    session = AsyncMock()

    async def handler(event, data):
        return "ok"

    result = await mw(handler, object(), {"session": session})

    assert result == "ok"
    session.rollback.assert_not_awaited()  # the outer DB middleware still commits


@pytest.mark.asyncio
async def test_missing_session_does_not_crash():
    mw = GlobalErrorMiddleware()

    async def handler(event, data):
        raise RuntimeError("boom")

    # No "session" injected (e.g. a non-DB update) — must degrade gracefully.
    result = await mw(handler, object(), {})
    assert result is None
