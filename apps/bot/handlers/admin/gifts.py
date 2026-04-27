from __future__ import annotations

from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import BulkGiftStates
from apps.bot.utils.messaging import safe_edit_or_send
from core.texts import AdminButtons
from models.user import User
from models.xui import XUIServerRecord
from repositories.audit import AuditLogRepository
from services.admin_gifts import grant_bulk_subscription_gift


router = Router(name="admin-gifts")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class GiftScopeCallback(CallbackData, prefix="gift_scope"):
    status_scope: str


class GiftServerCallback(CallbackData, prefix="gift_srv"):
    server_id: str


class GiftTypeCallback(CallbackData, prefix="gift_type"):
    gift_type: str


class GiftConfirmCallback(CallbackData, prefix="gift_ok"):
    action: str


@router.callback_query(F.data == "admin:gifts")
async def gift_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="فقط کانفیگ‌های فعال", callback_data=GiftScopeCallback(status_scope="active").pack())
    builder.button(text="همه کانفیگ‌ها", callback_data=GiftScopeCallback(status_scope="all").pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "🎁 هدیه گروهی به کانفیگ‌ها\n\nابتدا مشخص کنید هدیه به کدام کانفیگ‌ها اعمال شود.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(GiftScopeCallback.filter())
async def gift_scope_selected(
    callback: CallbackQuery,
    callback_data: GiftScopeCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await state.update_data(status_scope=callback_data.status_scope)
    result = await session.execute(
        select(XUIServerRecord)
        .where(XUIServerRecord.health_status != "deleted")
        .order_by(XUIServerRecord.created_at.asc())
        .limit(30)
    )
    servers = list(result.scalars().all())

    builder = InlineKeyboardBuilder()
    builder.button(text="همه سرورها", callback_data=GiftServerCallback(server_id="all").pack())
    for server in servers:
        builder.button(text=server.name, callback_data=GiftServerCallback(server_id=str(server.id)).pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:gifts")
    builder.adjust(1)
    await safe_edit_or_send(callback, "حالا محدوده سرور را انتخاب کنید.", reply_markup=builder.as_markup())


@router.callback_query(GiftServerCallback.filter())
async def gift_server_selected(
    callback: CallbackQuery,
    callback_data: GiftServerCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(server_id=None if callback_data.server_id == "all" else callback_data.server_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="هدیه زمان", callback_data=GiftTypeCallback(gift_type="time").pack())
    builder.button(text="هدیه حجم", callback_data=GiftTypeCallback(gift_type="volume").pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:gifts")
    builder.adjust(1)
    await safe_edit_or_send(callback, "نوع هدیه را انتخاب کنید.", reply_markup=builder.as_markup())


@router.callback_query(GiftTypeCallback.filter())
async def gift_type_selected(
    callback: CallbackQuery,
    callback_data: GiftTypeCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(gift_type=callback_data.gift_type)
    await state.set_state(BulkGiftStates.waiting_for_amount)
    unit = "روز" if callback_data.gift_type == "time" else "گیگابایت"
    await safe_edit_or_send(
        callback,
        f"مقدار هدیه را به {unit} وارد کنید.\nبرای لغو /cancel را بزنید.",
    )


@router.message(BulkGiftStates.waiting_for_amount, F.text == "/cancel")
async def gift_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("لغو شد.")


@router.message(BulkGiftStates.waiting_for_amount)
async def gift_amount_entered(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    data = await state.get_data()
    gift_type = str(data.get("gift_type") or "")
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
        if gift_type == "time" and int(amount) != amount:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر بیشتر از صفر وارد کنید.")
        return

    await state.update_data(amount=amount)
    scope_label = "فقط فعال‌ها" if data.get("status_scope") == "active" else "همه کانفیگ‌ها"
    server_label = "همه سرورها" if not data.get("server_id") else f"سرور {str(data['server_id'])[:8]}"
    unit = "روز" if gift_type == "time" else "GB"
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ اعمال هدیه", callback_data=GiftConfirmCallback(action="apply").pack())
    builder.button(text="❌ لغو", callback_data=GiftConfirmCallback(action="cancel").pack())
    builder.adjust(1)
    await message.answer(
        "لطفاً تایید کنید:\n\n"
        f"محدوده کانفیگ: {scope_label}\n"
        f"محدوده سرور: {server_label}\n"
        f"نوع هدیه: {'زمان' if gift_type == 'time' else 'حجم'}\n"
        f"مقدار: {amount:g} {unit}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(GiftConfirmCallback.filter())
async def gift_confirm(
    callback: CallbackQuery,
    callback_data: GiftConfirmCallback,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    if callback_data.action != "apply":
        await state.clear()
        await safe_edit_or_send(callback, "لغو شد.")
        return

    data = await state.get_data()
    await state.clear()
    gift_type = str(data["gift_type"])
    amount = float(data["amount"])
    status_scope = str(data["status_scope"])
    server_id = UUID(str(data["server_id"])) if data.get("server_id") else None

    result = await grant_bulk_subscription_gift(
        session=session,
        gift_type=gift_type,
        amount=amount,
        status_scope=status_scope,
        server_id=server_id,
    )
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="bulk_subscription_gift",
        entity_type="subscription",
        entity_id=admin_user.id,
        payload={
            "gift_type": gift_type,
            "amount": amount,
            "status_scope": status_scope,
            "server_id": str(server_id) if server_id else None,
            "matched": result.matched_count,
            "updated": result.updated_count,
            "failed": result.failed_count,
        },
    )
    await session.flush()
    await safe_edit_or_send(
        callback,
        "✅ هدیه اعمال شد.\n\n"
        f"یافت‌شده: {result.matched_count}\n"
        f"موفق: {result.updated_count}\n"
        f"ناموفق: {result.failed_count}",
    )
