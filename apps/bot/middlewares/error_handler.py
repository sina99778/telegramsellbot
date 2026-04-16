"""
Global error handler middleware for the bot.
Catches unhandled exceptions in handlers and sends a user-friendly message.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ErrorEvent, Message, Update

logger = logging.getLogger(__name__)


class GlobalErrorMiddleware(BaseMiddleware):
    """
    Catches any unhandled exception in message/callback_query handlers
    and sends a user-friendly error message instead of failing silently.
    """

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            logger.error(
                "Unhandled exception in handler: %s", exc, exc_info=True
            )
            error_text = "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
            try:
                if isinstance(event, Message):
                    await event.answer(error_text)
                elif isinstance(event, CallbackQuery):
                    await event.answer(error_text, show_alert=True)
            except Exception:
                pass  # Can't even send the error message
            return None
