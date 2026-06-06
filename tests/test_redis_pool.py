"""Regression tests for the Redis connection-pool hardening.

Root cause of the renewal-time `MaxConnectionsError`: an unbounded, non-health-
checked pool plus a pub/sub listener that leaked one connection per reconnect.
These tests lock in the fix.
"""
from __future__ import annotations

import asyncio

import pytest
import redis.asyncio as aioredis

import core.redis as r
import core.cache_sync as cs


def test_shared_client_uses_bounded_blocking_health_checked_pool(monkeypatch):
    # Force a fresh build (the module caches a singleton).
    monkeypatch.setattr(r, "_redis_client", None)
    client = r.get_redis()
    pool = client.connection_pool

    # Blocking → waits for a free connection instead of raising under a burst.
    assert isinstance(pool, aioredis.BlockingConnectionPool)
    # Bounded → a leak/burst can't open unlimited sockets to the server.
    assert pool.max_connections == r.settings.redis_max_connections
    assert pool.max_connections < 1000  # definitely not the 2**31 default
    # Health-checked → dead connections are recycled, not accumulated.
    assert pool.connection_kwargs.get("health_check_interval") == 30

    monkeypatch.setattr(r, "_redis_client", None)  # don't leak into other tests


@pytest.mark.asyncio
async def test_listener_releases_pubsub_on_reconnect(monkeypatch):
    """The listener must close its PubSub (release the connection) on every
    reconnect — otherwise each Redis blip leaks a connection."""
    closed = {"count": 0}

    class FakePubSub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed["count"] += 1
            return False

        async def subscribe(self, _channel):
            raise RuntimeError("simulated redis drop")  # force a reconnect

        async def listen(self):  # pragma: no cover - never reached
            if False:
                yield None

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

    monkeypatch.setattr(cs, "get_redis", lambda: FakeRedis())

    # End the otherwise-infinite reconnect loop after the first retry sleep.
    async def _cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(cs.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await cs.run_cache_invalidation_listener()

    # __aexit__ ran exactly once → the leaked-per-reconnect connection is gone.
    assert closed["count"] == 1
