from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from core.config import settings
from core.texts import Buttons, Messages


def get_main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    base = settings.web_base_url.rstrip('/')
    # Telegram requires HTTPS for WebApp URLs
    if base.startswith('http://'):
        base = 'https://' + base[7:]
    miniapp_url = f"{base}/miniapp/"

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
        [
            KeyboardButton(text="📱 پنل کاربری", web_app=WebAppInfo(url=miniapp_url)),
        ],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="پنل مدیریت ⚙️")])
        
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=Messages.MENU_PLACEHOLDER,
    )
