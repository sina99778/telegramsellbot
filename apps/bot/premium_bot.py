from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.methods import TelegramMethod

from services.telegram.premium_emoji import apply_premium_emoji_to_method


class PremiumEmojiBot(Bot):
    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        default_parse_mode = getattr(getattr(self, "default", None), "parse_mode", None)
        await apply_premium_emoji_to_method(method, default_parse_mode=default_parse_mode)
        return await super().__call__(method, request_timeout=request_timeout)
