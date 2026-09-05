from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from apps.bot.handlers.user.start import _send_welcome_response


@pytest.mark.asyncio
async def test_welcome_response_retries_without_keyboard_after_keyboard_rejection():
    message = AsyncMock()
    keyboard = object()
    message.answer.side_effect = [
        TelegramBadRequest(method=None, message="reply markup is invalid"),
        None,
    ]

    await _send_welcome_response(message, "<b>Welcome</b>", keyboard)

    assert message.answer.await_args_list[0].args == ("<b>Welcome</b>",)
    assert message.answer.await_args_list[0].kwargs == {
        "reply_markup": keyboard,
        "parse_mode": "HTML",
    }
    assert message.answer.await_args_list[1].args == ("<b>Welcome</b>",)
    assert message.answer.await_args_list[1].kwargs == {"parse_mode": "HTML"}


@pytest.mark.asyncio
async def test_welcome_response_retries_plain_text_after_html_rejection():
    message = AsyncMock()
    message.answer.side_effect = [
        TelegramBadRequest(method=None, message="reply markup is invalid"),
        TelegramBadRequest(method=None, message="can't parse entities"),
        None,
    ]

    await _send_welcome_response(message, "<b>Welcome &amp; back</b>", object())

    assert message.answer.await_args_list[1].kwargs == {"parse_mode": "HTML"}
    assert message.answer.await_args_list[2].args == ("Welcome & back",)
    assert message.answer.await_args_list[2].kwargs == {}
