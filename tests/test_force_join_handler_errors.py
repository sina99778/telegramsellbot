"""Membership errors must never swallow or replay downstream handler errors."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import CallbackQuery, User
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from apps.bot.middlewares.force_join import ForceJoinMiddleware


@pytest.mark.parametrize(
    "error",
    [
        StaleDataError("fixture conflict"),
        ValueError("fixture failure"),
        TelegramBadRequest(
            method=GetChatMember(chat_id="@fixture", user_id=123), message="fixture error"
        ),
    ],
)
async def test_member_handler_failure_propagates_exactly_once(error):
    event = CallbackQuery(
        id="fixture", from_user=User(id=123, is_bot=False, first_name="Fixture"),
        chat_instance="fixture", data="fixture:view",
    )
    session = AsyncMock(spec=AsyncSession)
    handler = AsyncMock(side_effect=error)
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
    )
    gateway = SimpleNamespace(force_join_enabled=True, force_join_channel="@fixture")
    with (
        patch(
            "apps.bot.middlewares.force_join.AppSettingsRepository.get_gateway_settings",
            AsyncMock(return_value=gateway),
        ),
        pytest.raises(type(error)) as caught,
    ):
        await ForceJoinMiddleware()(handler, event, {"session": session, "bot": bot})
    assert caught.value is error
    handler.assert_awaited_once()
    bot.get_chat_member.assert_awaited_once()
