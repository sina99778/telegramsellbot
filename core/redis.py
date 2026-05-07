"""
Centralized Redis client for the application.
Uses redis.asyncio for async support.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import uuid4

import redis.asyncio as aioredis

from core.config import settings

logger = logging.getLogger(__name__)

_redis_client: aioredis.Redis | None = None

# Lua script for safe lock release — only deletes if the value matches owner_id
_RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def get_redis() -> aioredis.Redis:
    """Return the shared Redis client instance."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis() -> None:
    """Close the shared Redis connection pool."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


@asynccontextmanager
async def distributed_lock(
    key: str,
    ttl_seconds: int = 30,
) -> AsyncGenerator[bool, None]:
    """
    Async context manager for a distributed Redis lock.

    Uses an owner identifier to prevent releasing another process's lock
    when TTL expires before the work completes.

    Usage:
        async with distributed_lock("lock:renewal:user123:sub456") as acquired:
            if not acquired:
                return  # someone else holds the lock
            ... do work ...

    The lock is always released (deleted) on exit, even if an exception occurs.
    TTL ensures the lock is auto-released even if the process crashes.
    """
    redis = get_redis()
    owner_id = uuid4().hex
    acquired = await redis.set(key, owner_id, nx=True, ex=ttl_seconds)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            try:
                await redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, owner_id)
            except Exception as exc:
                logger.warning("Failed to release Redis lock %s: %s", key, exc)
