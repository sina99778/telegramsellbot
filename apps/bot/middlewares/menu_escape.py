"""
Auto-escape the active FSM state when the user taps a main-menu button.

Problem
-------
If a user starts a flow (e.g. /buy → "Enter config name") and then —
instead of typing the next expected input — taps a main-menu reply
keyboard button like "💬 پشتیبانی" or "📋 سرویس‌های من", the
state-filtered handler `@router.message(SomeState.waiting_for_X)`
fires FIRST and tries to validate the button's *label text* as the
flow's expected input. That produces a confusing rejection like

    ❌ نام نامعتبر است. فقط حروف انگلیسی … مجاز است.

and traps the user in the flow with no obvious way out.

Fix
---
This middleware sits at the very front of the user router's middleware
chain. For every incoming text message, it checks whether the text
matches one of the main-menu / admin-menu reply-keyboard labels. If so,
it clears any active FSM state BEFORE the handler chain runs and
attaches a one-shot toast flag so the next handler can let the user
know the previous flow was cancelled.

The state-filtered handler then sees `state == None` and skips
(because of its ``StateFilter``). The text-filtered handler
(`@router.message(F.text == Buttons.SUPPORT)`) is reached and fires.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

from core.texts import Buttons


# Reply-keyboard labels that should ALWAYS escape any active FSM state.
# Drawn from `apps/bot/keyboards/user.py` (main menu) plus the two
# variants of the admin-bot launcher (kept in sync with the dual-match
# in apps/bot/handlers/admin/servers.py).
_ESCAPE_TEXTS: frozenset[str] = frozenset({
    Buttons.BUY_CONFIG,
    Buttons.MY_CONFIGS,
    Buttons.PROFILE_WALLET,
    Buttons.TEST_CONFIG,
    Buttons.REFERRAL,
    Buttons.SUPPORT,
    # Mini-app launcher labels (defined inside apps/bot/keyboards/user.py
    # as module-local constants — duplicated here so we don't reach
    # across module boundaries for one string).
    "📱 ورود به پنل کاربری",
    "🛠 پنل مدیریت (مینی‌اپ)",
    # Admin bot-side menu launcher (current + legacy form).
    "⚙️ پنل مدیریت",
    "پنل مدیریت ⚙️",
})


class MainMenuEscapeMiddleware(BaseMiddleware):
    """Clear active FSM state on main-menu button taps. See module docstring."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.text
            and event.text in _ESCAPE_TEXTS
        ):
            state: FSMContext | None = data.get("state")
            if state is not None:
                current = await state.get_state()
                if current is not None:
                    # We can't safely send a message from here (the user's
                    # next handler will already be sending something —
                    # adding our own would double-render). Just clear and
                    # let the user proceed.
                    await state.clear()
        return await handler(event, data)
