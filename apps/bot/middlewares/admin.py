from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.user import UserRepository


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Allow access only to `admin` and `owner` users for protected routers.
    """

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
            return None

        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None or user.role not in {"admin", "owner"}:
            await _deny_access(event)
            return None

        data["admin_user"] = user
        return await handler(event, data)


def _extract_telegram_id(event: TelegramObject) -> int | None:
    if isinstance(event, Message) and event.from_user is not None:
        return event.from_user.id
    if isinstance(event, CallbackQuery) and event.from_user is not None:
        return event.from_user.id
    return None


async def _deny_access(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer("Permission denied.")
    elif isinstance(event, CallbackQuery):
        await event.answer("Permission denied.", show_alert=True)
