from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from core.texts import Buttons, Messages


def get_main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text=Buttons.BUY_CONFIG),
            KeyboardButton(text=Buttons.PROFILE_WALLET),
        ],
        [
            KeyboardButton(text=Buttons.SUPPORT),
            KeyboardButton(text=Buttons.MY_CONFIGS),
        ],
        [
            KeyboardButton(text=Buttons.REFERRAL),
            KeyboardButton(text=Buttons.TEST_CONFIG),
        ],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="پنل مدیریت ⚙️")])
        
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=Messages.MENU_PLACEHOLDER,
    )
