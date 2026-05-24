from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from core.config import settings
from core.miniapp_auth import create_miniapp_session_token
from core.texts import Buttons, Messages


# Labels used inside this module only. Kept here (not in core.texts.Buttons)
# because they don't route to a handler — the WebApp button is matched by
# Telegram itself, not by `F.text`.
_MINIAPP_LABEL = "📱 ورود به پنل کاربری"
_ADMIN_MINIAPP_LABEL = "🛠 پنل مدیریت (مینی‌اپ)"
_ADMIN_BOT_LABEL = "⚙️ پنل مدیریت"


def get_main_menu_keyboard(is_admin: bool = False, telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    """Build the main reply keyboard.

    Layout (4 rows for users, +2 rows for admins):

        Row 1: [        📱 ورود به پنل کاربری  (WebApp)        ]
        Row 2: [ 🛒 خرید سرویس  ] [ 📋 سرویس‌های من ]
        Row 3: [ 💰 حساب و کیف پول ] [ 🎁 سرویس تست رایگان ]
        Row 4: [ 🎉 دعوت دوستان ] [ 💬 پشتیبانی ]
        Row 5 (admin): [ 🛠 پنل مدیریت (مینی‌اپ)  (WebApp) ]
        Row 6 (admin): [ ⚙️ پنل مدیریت ]

    Design notes:
      * The Mini-App button is alone on row 1 so it reads as the
        primary CTA (largest tap target).
      * Transactional buttons (buy / my configs) sit on row 2 so a
        new buyer's eye lands on them right after the CTA.
      * Money buttons (wallet / free trial) live on row 3 — they're
        the "now or later" lane that doesn't always lead to a sale.
      * Growth buttons (referral / support) live on row 4 — useful
        but rarely the user's first goal.
      * Admin buttons are appended as a clearly separate block.
    """
    base = settings.web_base_url.rstrip('/')
    # Telegram requires HTTPS for WebApp URLs.
    if base.startswith('http://'):
        base = 'https://' + base[7:]
    miniapp_url = f"{base}/miniapp/"
    if telegram_id is not None:
        miniapp_url = f"{miniapp_url}?session={create_miniapp_session_token(telegram_id)}"

    keyboard: list[list[KeyboardButton]] = [
        # ── Primary CTA ──────────────────────────────────────────
        [KeyboardButton(text=_MINIAPP_LABEL, web_app=WebAppInfo(url=miniapp_url))],
        # ── Transactional ────────────────────────────────────────
        [
            KeyboardButton(text=Buttons.BUY_CONFIG),
            KeyboardButton(text=Buttons.MY_CONFIGS),
        ],
        # ── Money / free trial ───────────────────────────────────
        [
            KeyboardButton(text=Buttons.PROFILE_WALLET),
            KeyboardButton(text=Buttons.TEST_CONFIG),
        ],
        # ── Growth / support ─────────────────────────────────────
        [
            KeyboardButton(text=Buttons.REFERRAL),
            KeyboardButton(text=Buttons.SUPPORT),
        ],
    ]

    if is_admin:
        admin_url = (
            f"{miniapp_url}&page=admin" if "?" in miniapp_url
            else f"{miniapp_url}?page=admin"
        )
        keyboard.append([KeyboardButton(text=_ADMIN_MINIAPP_LABEL, web_app=WebAppInfo(url=admin_url))])
        keyboard.append([KeyboardButton(text=_ADMIN_BOT_LABEL)])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=Messages.MENU_PLACEHOLDER,
    )
