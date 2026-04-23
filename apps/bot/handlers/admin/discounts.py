from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
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


class DiscountDetailCallback(CallbackData, prefix="dc_detail"):
    action: str  # view, edit_percent, edit_expiry, toggle, delete
    discount_id: UUID


# ─── Discount List ────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:discounts")
async def admin_discounts_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = DiscountRepository(session)
    codes = await repo.list_active(limit=20)

    builder = InlineKeyboardBuilder()

    if codes:
        lines = ["📋 کدهای تخفیف فعال:\n"]
        for dc in codes:
            remaining = _format_remaining_time(dc.expires_at)
            lines.append(
                f"🏷 `{dc.code}` — {dc.discount_percent}% — "
                f"استفاده: {dc.used_count}/{dc.max_uses}"
                + (f" — ⏳ {remaining}" if remaining else "")
            )
            builder.button(
                text=f"✏️ {dc.code}",
                callback_data=DiscountDetailCallback(action="view", discount_id=dc.id).pack(),
            )
        text = "\n".join(lines)
    else:
        text = "📋 هیچ کد تخفیف فعالی وجود ندارد."

    builder.button(text="➕ ساخت کد تخفیف", callback_data="admin:discounts:create")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Discount Detail ─────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "view"))
async def view_discount_detail(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    repo = DiscountRepository(session)
    dc = await repo.get_by_id(callback_data.discount_id)
    if dc is None:
        await safe_edit_or_send(callback, "❌ کد تخفیف پیدا نشد.")
        return

    remaining = _format_remaining_time(dc.expires_at)
    status_text = "🟢 فعال" if dc.is_active else "🔴 غیرفعال"
    expires_text = dc.expires_at.strftime("%Y-%m-%d %H:%M") if dc.expires_at else "بدون محدودیت"

    text = (
        f"🏷 جزئیات کد تخفیف\n\n"
        f"📝 کد: `{dc.code}`\n"
        f"💰 درصد تخفیف: {dc.discount_percent}%\n"
        f"🔢 استفاده: {dc.used_count}/{dc.max_uses}\n"
        f"📊 وضعیت: {status_text}\n"
        f"📅 انقضا: {expires_text}\n"
    )
    if remaining:
        text += f"⏳ زمان باقیمانده: {remaining}\n"

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💰 تغییر درصد تخفیف",
        callback_data=DiscountDetailCallback(action="edit_percent", discount_id=dc.id).pack(),
    )
    builder.button(
        text="📅 تغییر تاریخ انقضا",
        callback_data=DiscountDetailCallback(action="edit_expiry", discount_id=dc.id).pack(),
    )
    builder.button(
        text="🔢 تغییر حداکثر استفاده",
        callback_data=DiscountDetailCallback(action="edit_max_uses", discount_id=dc.id).pack(),
    )
    toggle_text = "🔴 غیرفعال کردن" if dc.is_active else "🟢 فعال کردن"
    builder.button(
        text=toggle_text,
        callback_data=DiscountDetailCallback(action="toggle", discount_id=dc.id).pack(),
    )
    builder.button(
        text="🗑 حذف کد",
        callback_data=DiscountDetailCallback(action="delete", discount_id=dc.id).pack(),
    )
    builder.button(text=AdminButtons.BACK, callback_data="admin:discounts")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Toggle Active ────────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "toggle"))
async def toggle_discount(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    repo = DiscountRepository(session)
    dc = await repo.get_by_id(callback_data.discount_id)
    if dc is None:
        await safe_edit_or_send(callback, "❌ کد پیدا نشد.")
        return
    dc.is_active = not dc.is_active
    session.add(dc)
    await session.flush()
    # Re-render detail
    await view_discount_detail(callback, callback_data, session)


# ─── Delete ───────────────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "delete"))
async def delete_discount(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    repo = DiscountRepository(session)
    dc = await repo.get_by_id(callback_data.discount_id)
    if dc is None:
        await safe_edit_or_send(callback, "❌ کد پیدا نشد.")
        return
    await session.delete(dc)
    await session.flush()
    await safe_edit_or_send(callback, f"✅ کد `{dc.code}` حذف شد.")


# ─── Edit Percent ─────────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "edit_percent"))
async def edit_discount_percent_start(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(DiscountStates.waiting_for_edit_percent)
    await state.update_data(editing_discount_id=str(callback_data.discount_id))

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data=DiscountDetailCallback(action="view", discount_id=callback_data.discount_id).pack())
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "💰 درصد تخفیف جدید را وارد کنید (۱ تا ۱۰۰):",
        reply_markup=builder.as_markup(),
    )


@router.message(DiscountStates.waiting_for_edit_percent)
async def edit_discount_percent_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        percent = int(message.text.strip())
        if percent < 1 or percent > 100:
            raise ValueError
    except ValueError:
        await message.answer("عدد نامعتبر. درصد باید بین ۱ تا ۱۰۰ باشد.")
        return

    data = await state.get_data()
    await state.clear()

    repo = DiscountRepository(session)
    dc = await repo.get_by_id(UUID(data["editing_discount_id"]))
    if dc is None:
        await message.answer("❌ کد پیدا نشد.")
        return

    dc.discount_percent = percent
    session.add(dc)
    await session.flush()
    await message.answer(f"✅ درصد تخفیف کد `{dc.code}` به {percent}% تغییر کرد.")


# ─── Edit Expiry ──────────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "edit_expiry"))
async def edit_discount_expiry_start(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(DiscountStates.waiting_for_edit_expiry)
    await state.update_data(editing_discount_id=str(callback_data.discount_id))

    builder = InlineKeyboardBuilder()
    builder.button(text="♾ بدون محدودیت زمانی", callback_data=f"dc_clear_expiry:{callback_data.discount_id}")
    builder.button(text=AdminButtons.BACK, callback_data=DiscountDetailCallback(action="view", discount_id=callback_data.discount_id).pack())
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "📅 تعداد روز اعتبار جدید را وارد کنید.\n"
        "مثلاً `7` یعنی ۷ روز از الان.\n\n"
        "یا دکمه «بدون محدودیت» را بزنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("dc_clear_expiry:"))
async def clear_discount_expiry(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    discount_id = UUID(callback.data.split(":", 1)[1])
    repo = DiscountRepository(session)
    dc = await repo.get_by_id(discount_id)
    if dc is None:
        await safe_edit_or_send(callback, "❌ کد پیدا نشد.")
        return
    dc.expires_at = None
    session.add(dc)
    await session.flush()
    await safe_edit_or_send(callback, f"✅ محدودیت زمانی کد `{dc.code}` حذف شد.")


@router.message(DiscountStates.waiting_for_edit_expiry)
async def edit_discount_expiry_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        days = int(message.text.strip())
        if days < 1:
            raise ValueError
    except ValueError:
        await message.answer("عدد نامعتبر. حداقل ۱ روز وارد کنید.")
        return

    data = await state.get_data()
    await state.clear()

    repo = DiscountRepository(session)
    dc = await repo.get_by_id(UUID(data["editing_discount_id"]))
    if dc is None:
        await message.answer("❌ کد پیدا نشد.")
        return

    dc.expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    session.add(dc)
    await session.flush()
    await message.answer(f"✅ انقضای کد `{dc.code}` به {days} روز از الان تنظیم شد.")


# ─── Edit Max Uses ────────────────────────────────────────────────────────────


@router.callback_query(DiscountDetailCallback.filter(F.action == "edit_max_uses"))
async def edit_discount_max_uses_start(
    callback: CallbackQuery,
    callback_data: DiscountDetailCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(DiscountStates.waiting_for_edit_max_uses)
    await state.update_data(editing_discount_id=str(callback_data.discount_id))

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data=DiscountDetailCallback(action="view", discount_id=callback_data.discount_id).pack())
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "🔢 حداکثر تعداد استفاده جدید را وارد کنید:",
        reply_markup=builder.as_markup(),
    )


@router.message(DiscountStates.waiting_for_edit_max_uses)
async def edit_discount_max_uses_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        max_uses = int(message.text.strip())
        if max_uses < 1:
            raise ValueError
    except ValueError:
        await message.answer("عدد نامعتبر. حداقل ۱.")
        return

    data = await state.get_data()
    await state.clear()

    repo = DiscountRepository(session)
    dc = await repo.get_by_id(UUID(data["editing_discount_id"]))
    if dc is None:
        await message.answer("❌ کد پیدا نشد.")
        return

    dc.max_uses = max_uses
    # Re-activate if new max_uses > used_count
    if dc.used_count < max_uses:
        dc.is_active = True
    session.add(dc)
    await session.flush()
    await message.answer(f"✅ حداکثر استفاده کد `{dc.code}` به {max_uses} تغییر کرد.")


# ─── Create New ───────────────────────────────────────────────────────────────


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


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _format_remaining_time(expires_at: datetime | None) -> str:
    """Format remaining time until expiry as a human-readable string."""
    if expires_at is None:
        return ""
    
    now = datetime.now(timezone.utc)
    if expires_at <= now:
        return "منقضی شده ❌"
    
    diff = expires_at - now
    days = diff.days
    hours = diff.seconds // 3600
    
    if days > 0:
        return f"{days} روز و {hours} ساعت"
    elif hours > 0:
        minutes = (diff.seconds % 3600) // 60
        return f"{hours} ساعت و {minutes} دقیقه"
    else:
        minutes = diff.seconds // 60
        return f"{minutes} دقیقه"
