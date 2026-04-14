from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import SettingsStates
from core.texts import AdminButtons, AdminMessages
from repositories.settings import AppSettingsRepository

logger = logging.getLogger(__name__)

router = Router(name="admin-settings")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:bot_settings")
async def bot_settings_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    
    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()
    toman_rate = await settings_repo.get_toman_rate()
    
    text = AdminMessages.SETTINGS_MENU.format(
        price_per_gb=renewal_settings.price_per_gb,
        price_per_10_days=renewal_settings.price_per_10_days,
        toman_rate=f"{toman_rate:,}",
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر قیمت تمدید هر ۱ گیگ", callback_data="admin:settings:edit_gb")
    builder.button(text="تغییر قیمت تمدید هر ۱۰ روز", callback_data="admin:settings:edit_days")
    builder.button(text="💱 تغییر نرخ دلار به تومان", callback_data="admin:settings:edit_toman")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:settings:edit_gb")
async def edit_price_gb_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_price_gb)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await callback.message.edit_text(
        AdminMessages.ENTER_PRICE_PER_GB,
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_price_gb)
async def edit_price_gb_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        new_price = float(message.text.strip())
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_renewal_settings(price_per_gb=new_price)

    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:edit_days")
async def edit_price_days_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_price_days)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await callback.message.edit_text(
        AdminMessages.ENTER_PRICE_PER_10_DAYS,
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_price_days)
async def edit_price_days_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        new_price = float(message.text.strip())
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_renewal_settings(price_per_10_days=new_price)

    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:edit_toman")
async def edit_toman_rate_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_toman_rate)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await callback.message.edit_text(
        "💱 نرخ فعلی دلار به تومان را وارد کنید.\n"
        "مثال: `85000`",
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_toman_rate)
async def edit_toman_rate_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        rate = int(message.text.strip().replace(",", ""))
        if rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد صحیح مثبت وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.set_toman_rate(rate)

    await state.clear()
    await message.answer(f"✅ نرخ دلار به تومان به {rate:,} تنظیم شد.")
