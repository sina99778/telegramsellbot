from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from core.config import settings
from core.miniapp_auth import create_miniapp_session_token
from core.texts import Buttons, Messages


def get_main_menu_keyboard(is_admin: bool = False, telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    base = settings.web_base_url.rstrip('/')
    # Telegram requires HTTPS for WebApp URLs
    if base.startswith('http://'):
        base = 'https://' + base[7:]
    miniapp_url = f"{base}/miniapp/"
    if telegram_id is not None:
        miniapp_url = f"{miniapp_url}?session={create_miniapp_session_token(telegram_id)}"

    keyboard = [
        [
            KeyboardButton(text="📱 ورود به مینی‌اپ (پیشنهادی)", web_app=WebAppInfo(url=miniapp_url)),
        ],
        [
            KeyboardButton(text=Buttons.BUY_CONFIG),
            KeyboardButton(text=Buttons.MY_CONFIGS),
        ],
        [
            KeyboardButton(text=Buttons.PROFILE_WALLET),
            KeyboardButton(text=Buttons.TEST_CONFIG),
        ],
        [
            KeyboardButton(text=Buttons.REFERRAL),
            KeyboardButton(text=Buttons.SUPPORT),
        ],
    ]
    if is_admin:
        admin_url = f"{miniapp_url}&page=admin" if "?" in miniapp_url else f"{miniapp_url}?page=admin"
        keyboard.append([KeyboardButton(text="⚡ پنل مدیریت ادمین (مینی‌اپ)", web_app=WebAppInfo(url=admin_url))])
        keyboard.append([KeyboardButton(text="⚙️ پنل مدیریت (ربات)")])
        
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="برای شروع وارد مینی‌اپ شوید...",
    )
