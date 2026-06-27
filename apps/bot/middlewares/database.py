from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.database import AsyncSessionFactory


class DatabaseSessionMiddleware(BaseMiddleware):
    """
    Inject a fresh SQLAlchemy async session into each update handler.

    The middleware commits on success, rolls back on failure, and closes the
    session automatically so handlers can just declare `session: AsyncSession`.

    IMPORTANT: GlobalErrorMiddleware (registered on message/callback_query)
    catches handler exceptions, rolls back the session, and returns None.
    When that happens, this middleware sees a "successful" return (no exception)
    but the session is already rolled back. We detect this via a flag
    ``_error_handled`` set by the error middleware, OR by checking the
    sync_session state, and skip the commit.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionFactory,
    ) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session

            try:
                result = await handler(event, data)

                # If the error handler already rolled back, don't commit.
                if getattr(session, "_error_handled", False):
                    return result

                # Extra safety: check if session is in a usable state.
                # After a rollback the sync_session may be in a state where
                # commit() would raise PendingRollbackError.
                try:
                    if not session.is_active:
                        return result
                except Exception:
                    pass

                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

