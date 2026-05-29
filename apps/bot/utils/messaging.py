"""Shared utility for safe message editing in callback handlers."""
from __future__ import annotations

import html
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
    a new message. Prevents message spam on back/button presses.

    The bot's default parse_mode is HTML, so a message carrying a stray
    ``<`` (e.g. an exception repr like ``<class '...'>`` interpolated into
    an error string) makes Telegram reject it with
    ``can't parse entities: Unsupported start tag``. When that happens the
    user gets NOTHING — the bot looks frozen. To prevent that, every send
    path below has a last-resort retry that HTML-escapes the text, so the
    message always lands (as literal text)."""
    if callback.message is None:
        return

    kwargs: dict[str, Any] = {"reply_markup": reply_markup}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode

    async def _attempt(send, body: str) -> bool:
        try:
            await send(body, **kwargs)
            return True
        except Exception:
            return False

    # 1) edit as-is  2) edit escaped  3) delete+send as-is  4) send escaped
    if await _attempt(callback.message.edit_text, text):
        return
    if await _attempt(callback.message.edit_text, html.escape(text)):
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    if await _attempt(callback.message.answer, text):
        return
    if await _attempt(callback.message.answer, html.escape(text)):
        return
    logger.warning("Failed to send message even after HTML-escaping: %.80s", text)
