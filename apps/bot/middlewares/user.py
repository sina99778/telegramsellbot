from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import utcnow
from core.texts import Messages
from repositories.user import UserRepository


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if not isinstance(session, AsyncSession):
            return await handler(event, data)

        telegram_id = _extract_telegram_id(event)
        if telegram_id is None:
            return await handler(event, data)

        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None:
            return await handler(event, data)

        if user.status == "banned":
            await _deny_user(event)
            return None

        user.last_seen_at = utcnow()
        data["current_user"] = user
        return await handler(event, data)


def _extract_telegram_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user is not None:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user is not None:
        return event.from_user.id
    return None


async def _deny_user(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer(Messages.ACCESS_DENIED)
    elif isinstance(event, CallbackQuery):
        await event.answer(Messages.ACCESS_DENIED, show_alert=True)
