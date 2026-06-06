"""
Cross-process settings-cache sync via Redis pub/sub.

The bot, worker, and api run as SEPARATE processes, each with its own in-memory
settings caches (button styles, premium emoji, text templates). Before this,
changing a setting in one process (e.g. the web dashboard) left the others
serving a STALE cache until they were restarted.

Now every settings write calls `invalidate(name)`, which refreshes the cache
locally AND publishes the name on a Redis channel. Each process runs
`run_cache_invalidation_listener()` and refreshes the matching cache on receipt
— so a change anywhere reaches every process within milliseconds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from core.redis import get_redis

logger = logging.getLogger(__name__)

_CHANNEL = "cache:invalidate"
_handlers: dict[str, Callable[[], Awaitable[None]]] = {}


def register_cache(name: str, handler: Callable[[], Awaitable[None]]) -> None:
    """Register an async handler that clears (and re-primes) the named cache."""
    _handlers[name] = handler


async def _run(name: str) -> None:
    handler = _handlers.get(name)
    if handler is None:
        return
    try:
        await handler()
        logger.info("cache '%s' refreshed", name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache handler '%s' failed: %s", name, exc)


async def publish(name: str) -> None:
    """Tell the OTHER processes to refresh the named cache (use when the local
    cache was already refreshed in-line)."""
    try:
        await get_redis().publish(_CHANNEL, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache publish failed for '%s': %s", name, exc)


async def invalidate(name: str) -> None:
    """Refresh the named cache locally AND signal the other processes."""
    await _run(name)
    await publish(name)


def register_default_caches() -> None:
    """Register the shared settings caches. Lazy imports keep it safe to call
    from any process (bot/worker/api)."""

    async def _button_style() -> None:
        from apps.bot.utils.button_style import (
            clear_button_style_cache,
            prime_button_style_cache,
        )
        clear_button_style_cache()
        await prime_button_style_cache()

    async def _premium_emoji() -> None:
        from apps.bot.utils.button_style import (
            clear_premium_icon_cache,
            prime_premium_icon_cache,
        )
        from services.telegram.premium_emoji import clear_premium_emoji_cache
        clear_premium_emoji_cache()
        clear_premium_icon_cache()
        await prime_premium_icon_cache()

    async def _text_templates() -> None:
        from services.text_templates import (
            clear_text_template_cache,
            prime_text_template_cache,
        )
        clear_text_template_cache()
        await prime_text_template_cache()

    register_cache("button_style", _button_style)
    register_cache("premium_emoji", _premium_emoji)
    register_cache("text_templates", _text_templates)


async def run_cache_invalidation_listener() -> None:
    """Background task: refresh local caches when another process changes a
    setting. Reconnects on failure; cancel to stop.

    The PubSub is entered with `async with` so its dedicated Redis connection is
    ALWAYS released back to the pool on every loop iteration — including on a
    reconnect. The previous version created a new pubsub on each retry without
    closing the old one, leaking one connection per reconnect until the pool was
    exhausted (surfacing elsewhere as MaxConnectionsError).
    """
    while True:
        try:
            async with get_redis().pubsub() as pubsub:
                await pubsub.subscribe(_CHANNEL)
                logger.info("cache invalidation listener subscribed (%s)", _CHANNEL)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    name = message.get("data")
                    if isinstance(name, (bytes, bytearray)):
                        name = name.decode()
                    await _run(str(name))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache listener error (retry in 5s): %s", exc)
            await asyncio.sleep(5)
