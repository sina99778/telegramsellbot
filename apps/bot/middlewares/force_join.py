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

        channel = gw.force_join_channel.strip()

        # Check membership
        try:
            bot = data.get("bot") or (event.bot if hasattr(event, "bot") else None)
            if bot is None:
                return await handler(event, data)

            member = await bot.get_chat_member(chat_id=channel, user_id=telegram_id)
            if member.status in ("member", "administrator", "creator"):
                return await handler(event, data)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("Force join check failed for channel %s: %s", channel, exc)
            # If bot can't check, let user through
            return await handler(event, data)
        except Exception as exc:
            logger.warning("Force join check error: %s", exc)
            return await handler(event, data)

        # User is NOT a member — block and show join button
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
