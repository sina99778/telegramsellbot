"""
Force channel join middleware.
Checks if user is a member of the required channel before processing any request.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.settings import AppSettingsRepository

logger = logging.getLogger(__name__)


def _normalize_channel(raw: str) -> str:
    """Normalise a force-join channel identifier for the Telegram API.

    A public channel may be stored without the leading "@" (a common operator
    slip) — get_chat_member then fails. Numeric IDs (-100…) are left as-is.
    """
    ch = (raw or "").strip()
    if "t.me/" in ch:  # operator pasted a full invite link
        tail = ch.split("t.me/", 1)[1].strip("/").split("/")[0].split("?")[0]
        ch = ("@" + tail) if tail and not tail.startswith("+") else ch
    elif ch and not ch.startswith("@") and not ch.lstrip("-").isdigit():
        ch = "@" + ch
    return ch


class ForceJoinMiddleware(BaseMiddleware):
    """Middleware that blocks bot usage until user joins the required channel."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if not isinstance(session, AsyncSession):
            return await handler(event, data)

        # Extract telegram_id
        telegram_id = None
        if isinstance(event, Message) and event.from_user:
            telegram_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            telegram_id = event.from_user.id

        if telegram_id is None:
            return await handler(event, data)

        # Allow /start command to go through (user might be joining for first time)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        # Allow callback for checking membership
        if isinstance(event, CallbackQuery) and event.data == "force_join:check":
            return await handler(event, data)

        # Check settings
        try:
            gw = await AppSettingsRepository(session).get_gateway_settings()
        except Exception:
            return await handler(event, data)

        if not gw.force_join_enabled or not gw.force_join_channel:
            return await handler(event, data)

        channel = _normalize_channel(gw.force_join_channel)

        # Check membership. If the bot can't actually verify membership we
        # fail CLOSED — otherwise an admin who accidentally removes the bot
        # from the channel silently turns off the force-join requirement
        # for the whole user base.
        bot = data.get("bot") or (event.bot if hasattr(event, "bot") else None)
        if bot is None:
            return await handler(event, data)

        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=telegram_id)
            if member.status in ("member", "administrator", "creator"):
                return await handler(event, data)
            # A definitive non-member status (left / kicked) → fall through and
            # prompt them to join.
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # The bot CAN'T query the channel — almost always because it isn't an
            # ADMIN of the force-join channel (or the channel is mis-set). That's
            # an OPERATOR misconfiguration, not the user's fault. Failing closed
            # here blocks the entire user base forever (including paying customers
            # mid-renewal), so we fail OPEN and log loudly instead.
            logger.error(
                "Force join: cannot verify membership for channel %s — letting the "
                "user through. FIX: make the bot an ADMIN of the channel. (%s)",
                channel, exc,
            )
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Force join: unexpected check error for channel %s — letting the user "
                "through: %s", channel, exc,
            )
            return await handler(event, data)

        # User is definitively NOT a member — block and show the join button.
        channel_link = channel if channel.startswith("@") else channel
        # Try to get channel invite link
        try:
            chat = await bot.get_chat(channel)
            invite_url = chat.invite_link or f"https://t.me/{channel.lstrip('@')}"
        except Exception:
            invite_url = f"https://t.me/{channel.lstrip('@')}"

        builder = InlineKeyboardBuilder()
        builder.button(text="📢 عضویت در کانال", url=invite_url)
        builder.button(text="✅ عضو شدم", callback_data="force_join:check")
        builder.adjust(1)

        text = (
            "⚠️ <b>عضویت در کانال الزامی است!</b>\n\n"
            "برای استفاده از ربات، ابتدا در کانال زیر عضو شوید:\n\n"
            f"📢 {channel_link}\n\n"
            "بعد از عضویت، دکمه «✅ عضو شدم» را بزنید."
        )

        if isinstance(event, Message):
            await event.answer(text, reply_markup=builder.as_markup())
        elif isinstance(event, CallbackQuery):
            await event.answer("ابتدا در کانال عضو شوید!", show_alert=True)
            try:
                await event.message.edit_text(text, reply_markup=builder.as_markup())
            except Exception:
                pass

        return None
