"""Shared utility for safe message editing in callback handlers."""
from __future__ import annotations

import logging
from typing import Any

from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def safe_edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = None,
) -> None:
    """Try to edit the callback message. If editing fails, fall back to sending
    a new message. Prevents message spam on back/button presses."""
    if callback.message is None:
        return

    kwargs: dict[str, Any] = {"reply_markup": reply_markup}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode

    try:
        await callback.message.edit_text(text, **kwargs)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await callback.message.answer(text, **kwargs)
        except Exception as exc:
            logger.warning("Failed to send fallback message: %s", exc)
