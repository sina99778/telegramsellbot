from __future__ import annotations

import socket
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import TelegramMethod

from services.telegram.premium_emoji import apply_premium_emoji_to_method


class IPv4AiohttpSession(AiohttpSession):
    """Force IPv4 for Telegram API calls.

    Some hosts (e.g. Hetzner with Docker) resolve api.telegram.org to an IPv6
    address but have no working IPv6 route out of the container, so aiohttp's
    happy-eyeballs connect hangs and every request times out. Restricting the
    connector to AF_INET makes requests use the provably-working IPv4 path.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connector_init["family"] = socket.AF_INET


class PremiumEmojiBot(Bot):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("session", IPv4AiohttpSession())
        super().__init__(**kwargs)

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        default_parse_mode = getattr(getattr(self, "default", None), "parse_mode", None)
        await apply_premium_emoji_to_method(method, default_parse_mode=default_parse_mode)
        return await super().__call__(method, request_timeout=request_timeout)
