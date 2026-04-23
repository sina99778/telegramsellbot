"""
User Referral Handler.

Provides:
- View personal referral link
- View referral stats (how many referred, total bonus earned)
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.utils.messaging import safe_edit_or_send
from core.texts import Buttons
from models.user import User
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository

logger = logging.getLogger(__name__)

router = Router(name="user-referral")


@router.message(F.text == Buttons.REFERRAL)
async def referral_menu_handler(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("❌ حساب شما پیدا نشد. لطفاً /start را بزنید.")
        return

    await _show_referral_menu(message, user, session)


@router.callback_query(F.data == "referral:menu")
async def referral_menu_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "❌ حساب شما پیدا نشد.")
        return

    await _show_referral_menu(callback, user, session)


async def _show_referral_menu(
    event: Message | CallbackQuery,
    user: User,
    session: AsyncSession,
) -> None:
    settings_repo = AppSettingsRepository(session)
    ref_settings = await settings_repo.get_referral_settings()

    if not ref_settings.enabled:
        text = "🔗 سیستم رفرال در حال حاضر غیرفعال است."
        if isinstance(event, CallbackQuery):
            await safe_edit_or_send(event, text)
        else:
            await event.answer(text)
        return

    # Generate or get ref code
    if not user.ref_code:
        import secrets
        user.ref_code = secrets.token_hex(4)
        session.add(user)
        await session.flush()

    # Get bot username for the deep link
    bot_info = None
    if isinstance(event, CallbackQuery):
        bot_info = await event.bot.me()
    elif isinstance(event, Message):
        bot_info = await event.bot.me()

    bot_username = bot_info.username if bot_info else "YourBot"
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.ref_code}"

    # Count referrals
    total_referrals = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.referred_by_user_id == user.id)
        ) or 0
    )

    text = (
        "🔗 سیستم معرفی دوستان\n\n"
        f"📎 لینک دعوت شما:\n<code>{ref_link}</code>\n\n"
        f"👥 تعداد افراد دعوت‌شده: {total_referrals} نفر\n"
        f"💰 پاداش هر معرفی: {ref_settings.referrer_bonus_usd:.2f} دلار\n"
    )

    if ref_settings.referee_bonus_usd > 0:
        text += f"🎁 پاداش دعوت‌شده: {ref_settings.referee_bonus_usd:.2f} دلار\n"

    text += (
        "\n📢 لینک بالا را برای دوستانتان ارسال کنید.\n"
        "بعد از اولین خرید هر دعوت‌شده، پاداش شما به کیف پول واریز می‌شود."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📋 کپی لینک", url=ref_link)
    builder.button(text="❌ بستن", callback_data="purchase:cancel")
    builder.adjust(1)

    if isinstance(event, CallbackQuery):
        await safe_edit_or_send(event, text, reply_markup=builder.as_markup())
    else:
        await event.answer(text, reply_markup=builder.as_markup())
