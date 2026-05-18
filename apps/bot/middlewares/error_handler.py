"""
Global error handler middleware for the bot.
Catches unhandled exceptions in handlers and sends a user-friendly message.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


class GlobalErrorMiddleware(BaseMiddleware):
    """Catch unhandled exceptions and reply with a useful message.

    What "useful" means:
      * The user gets a short Persian message explaining the situation.
      * A short tracking code is shown so they can quote it to support.
      * The same code is in the server log so an operator can find the
        traceback in seconds.
      * FloodWait (Telegram rate-limit) gets its own message because it's
        the most common transient error and "خطایی رخ داد" hides the fix
        ("just wait N seconds").
    """

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            msg = (
                "⏸️ درخواست‌های زیادی فرستادید.\n"
                f"لطفاً <b>{wait}</b> ثانیه صبر کنید و دوباره تلاش کنید."
            )
            await _safe_reply(event, msg)
            return None
        except Exception as exc:
            # Short trace code so user + log can be linked.
            trace_code = secrets.token_hex(3).upper()
            user_id = _extract_user_id(event)
            logger.error(
                "Unhandled handler exception [trace=%s user=%s type=%s]: %s",
                trace_code, user_id, type(event).__name__, exc, exc_info=True,
            )
            err_class = type(exc).__name__
            error_text = (
                "⚠️ <b>خطایی پیش آمد</b>\n\n"
                "اگر مشکل ادامه داشت، با پشتیبانی تماس بگیرید و کد زیر را اعلام کنید:\n"
                f"کد پیگیری: <code>{trace_code}</code>\n"
                f"نوع: <code>{err_class}</code>\n\n"
                "/support"
            )
            await _safe_reply(event, error_text)
            return None


def _extract_user_id(event: Any) -> int | None:
    if isinstance(event, (Message, CallbackQuery)) and event.from_user:
        return event.from_user.id
    return None


async def _safe_reply(event: Any, text: str) -> None:
    try:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            # show_alert=True gives a modal popup which can't be ignored.
            await event.answer(text[:200], show_alert=True)
            if event.message:
                try:
                    await event.message.answer(text)
                except Exception:
                    pass
    except Exception:
        # If even the error reply fails, give up silently — better than a
        # crash loop.
        pass
