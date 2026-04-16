from __future__ import annotations

import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import DiscountStates
from core.texts import AdminButtons, AdminMessages
from repositories.discount import DiscountRepository
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-discounts")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:discounts")
async def admin_discounts_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = DiscountRepository(session)
    codes = await repo.list_active(limit=20)

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ ساخت کد تخفیف", callback_data="admin:discounts:create")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    if not codes:
        text = "📋 هیچ کد تخفیف فعالی وجود ندارد."
    else:
        lines = ["📋 کدهای تخفیف فعال:\n"]
        for dc in codes:
            lines.append(
                f"🏷 `{dc.code}` — {dc.discount_percent}% — "
                f"استفاده: {dc.used_count}/{dc.max_uses}"
            )
        text = "\n".join(lines)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:discounts:create")
async def create_discount_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # Auto-generate a random code suggestion
    suggested = secrets.token_hex(4).upper()
    await state.set_state(DiscountStates.waiting_for_code)
    await safe_edit_or_send(callback, 
        f"کد تخفیف را وارد کنید (یا از کد پیشنهادی استفاده کنید):\n\n"
        f"پیشنهاد: `{suggested}`",
    )


@router.message(DiscountStates.waiting_for_code)
async def create_discount_code_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    code = message.text.strip().upper()

    # Check for duplicate
    repo = DiscountRepository(session)
    existing = await repo.get_by_code(code)
    if existing:
        await message.answer("❌ این کد قبلاً وجود دارد. لطفاً کد دیگری وارد کنید.")
        return

    await state.update_data(code=code)
    await state.set_state(DiscountStates.waiting_for_percent)
    await message.answer("درصد تخفیف را وارد کنید (مثلاً 20 برای ۲۰ درصد):")


@router.message(DiscountStates.waiting_for_percent)
async def create_discount_percent_entered(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        percent = int(message.text.strip())
        if percent < 1 or percent > 100:
            raise ValueError
    except ValueError:
        await message.answer("عدد نامعتبر است. درصد باید بین ۱ تا ۱۰۰ باشد.")
        return

    await state.update_data(percent=percent)
    await state.set_state(DiscountStates.waiting_for_max_uses)
    await message.answer("حداکثر تعداد استفاده از این کد چند بار باشد؟ (مثلاً 10):")


@router.message(DiscountStates.waiting_for_max_uses)
async def create_discount_max_uses_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        max_uses = int(message.text.strip())
        if max_uses < 1:
            raise ValueError
    except ValueError:
        await message.answer("عدد نامعتبر است. حداقل ۱ بار استفاده.")
        return

    data = await state.get_data()
    await state.clear()

    repo = DiscountRepository(session)
    dc = await repo.create_code(
        code=data["code"],
        discount_percent=data["percent"],
        max_uses=max_uses,
    )

    await message.answer(
        f"✅ کد تخفیف ساخته شد:\n\n"
        f"🏷 کد: `{dc.code}`\n"
        f"💰 تخفیف: {dc.discount_percent}%\n"
        f"🔢 حداکثر استفاده: {dc.max_uses}\n"
    )
