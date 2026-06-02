"""
`MenuText` — an aiogram filter that matches a reply-menu button by its text,
ignoring a leading emoji.

Reply-keyboard buttons send their TEXT as a message, and handlers route on it
(F.text == "🛒 خرید سرویس"). When premium-emoji icons are active we move a
button's leading emoji into `icon_custom_emoji_id` and strip it from the text,
so the sent text becomes "خرید سرویس". Matching with `MenuText(...)` (which
strips the leading emoji on BOTH sides before comparing) makes the button route
correctly whether or not the emoji was stripped — and also if the user types
the label by hand.
"""
from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import Message

from apps.bot.utils.button_style import strip_leading_emoji


class MenuText(Filter):
    def __init__(self, *labels: str) -> None:
        self._targets = {strip_leading_emoji(label) for label in labels if label}

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False
        return strip_leading_emoji(message.text) in self._targets
