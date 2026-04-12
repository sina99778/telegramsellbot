from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛍 Buy Config"),
                KeyboardButton(text="👤 My Profile / Wallet"),
            ],
            [
                KeyboardButton(text="🛠 Support"),
                KeyboardButton(text="🎁 Free Trial"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option",
    )
