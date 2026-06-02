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


async def safe_edit_caption_or_text(
    callback: CallbackQuery,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = None,
) -> None:
    """Like ``safe_edit_or_send`` but it NEVER deletes the message — so a
    receipt PHOTO is preserved.

    When the callback message carries a photo (e.g. a card-to-card receipt
    sent to the admin), editing the *text* is impossible, and the old
    ``safe_edit_or_send`` path would ``delete()`` the message — wiping the
    receipt out of the chat. Here we edit the photo's CAPTION instead, so the
    image stays put. For a plain text message we edit the text. On any
    failure we send a NEW reply rather than deleting the original.
    """
    msg = callback.message
    if msg is None:
        return

    kwargs: dict[str, Any] = {"reply_markup": reply_markup}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode

    has_photo = bool(getattr(msg, "photo", None))

    if has_photo:
        # Telegram caption hard limit is 1024 chars.
        cap = text if len(text) <= 1024 else (text[:1015] + "\n…")
        for body in (cap, html.escape(cap)):
            try:
                await msg.edit_caption(caption=body, **kwargs)
                return
            except Exception:
                continue
        # Could not edit the caption — reply with a new message but KEEP the
        # photo (do NOT delete).
        for body in (text, html.escape(text)):
            try:
                await msg.answer(body, **kwargs)
                return
            except Exception:
                continue
        logger.warning("safe_edit_caption_or_text: photo path failed: %.80s", text)
        return

    for body in (text, html.escape(text)):
        try:
            await msg.edit_text(body, **kwargs)
            return
        except Exception:
            continue
    for body in (text, html.escape(text)):
        try:
            await msg.answer(body, **kwargs)
            return
        except Exception:
            continue
    logger.warning("safe_edit_caption_or_text: all attempts failed: %.80s", text)
