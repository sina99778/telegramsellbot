"""
Config Transfer Handler.
Allows users to transfer a subscription to another bot user.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.handlers.user.my_configs import MyConfigCallback
from apps.bot.utils.messaging import safe_edit_or_send
from models.subscription import Subscription
from models.user import User
from repositories.user import UserRepository

logger = logging.getLogger(__name__)

router = Router(name="user-transfer")


class TransferStates(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_confirmation = State()


class TransferConfirmCallback(CallbackData, prefix="txcf"):
    action: str  # 'yes' or 'no'


@router.callback_query(MyConfigCallback.filter(F.action == "transfer"))
async def transfer_start(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Start the config transfer process."""
    if callback.from_user is None:
        return
    await callback.answer()

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "❌ حساب شما یافت نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
            Subscription.status.in_(["active", "pending_activation"]),
        )
    )
    if sub is None:
        await safe_edit_or_send(callback, "❌ سرویس فعالی برای انتقال یافت نشد.")
        return

    await state.update_data(transfer_sub_id=str(sub.id))
    await state.set_state(TransferStates.waiting_for_recipient)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ لغو", callback_data="transfer:cancel")
    builder.adjust(1)

    await safe_edit_or_send(
        callback,
        "🔄 <b>انتقال کانفیگ</b>\n\n"
        "لطفاً آی‌دی عددی تلگرام یا یوزرنیم (بدون @) کاربر مقصد را ارسال کنید.\n\n"
        "⚠️ کاربر مقصد باید عضو ربات باشد.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "transfer:cancel")
async def transfer_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit_or_send(callback, "❌ انتقال لغو شد.")


@router.message(TransferStates.waiting_for_recipient)
async def transfer_recipient_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User enters recipient identifier."""
    if not message.text or message.from_user is None:
        return

    input_text = message.text.strip().lstrip("@")

    # Try to find recipient
    recipient = None

    # Try as numeric telegram ID
    try:
        tg_id = int(input_text)
        recipient = await UserRepository(session).get_by_telegram_id(tg_id)
    except ValueError:
        # Try as username
        recipient = await session.scalar(
            select(User).where(User.username == input_text)
        )

    if recipient is None:
        await message.answer("❌ کاربری با این مشخصات در ربات پیدا نشد. لطفاً دوباره تلاش کنید.")
        return

    if recipient.telegram_id == message.from_user.id:
        await message.answer("❌ نمی‌توانید کانفیگ را به خودتان انتقال دهید!")
        return

    # Store recipient info
    await state.update_data(
        recipient_user_id=str(recipient.id),
        recipient_telegram_id=recipient.telegram_id,
        recipient_name=recipient.first_name or recipient.username or str(recipient.telegram_id),
    )
    await state.set_state(TransferStates.waiting_for_confirmation)

    recipient_display = f"@{recipient.username}" if recipient.username else f"ID: {recipient.telegram_id}"

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید انتقال", callback_data=TransferConfirmCallback(action="yes").pack())
    builder.button(text="❌ لغو", callback_data=TransferConfirmCallback(action="no").pack())
    builder.adjust(1)

    await message.answer(
        "🔄 <b>تأیید انتقال</b>\n\n"
        f"👤 کاربر مقصد: <b>{recipient_display}</b>\n"
        f"📛 نام: {recipient.first_name or '-'}\n\n"
        "⚠️ <b>توجه:</b> بعد از انتقال، دسترسی شما به این کانفیگ <u>حذف</u> می‌شود.\n\n"
        "آیا از انتقال مطمئن هستید؟",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(TransferConfirmCallback.filter(F.action == "no"))
async def transfer_rejected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit_or_send(callback, "❌ انتقال لغو شد.")


@router.callback_query(TransferConfirmCallback.filter(F.action == "yes"))
async def transfer_confirmed(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Execute the transfer."""
    if callback.from_user is None:
        return
    await callback.answer()

    data = await state.get_data()
    await state.clear()

    sub_id_str = data.get("transfer_sub_id")
    recipient_user_id_str = data.get("recipient_user_id")
    recipient_telegram_id = data.get("recipient_telegram_id")
    recipient_name = data.get("recipient_name", "?")

    if not sub_id_str or not recipient_user_id_str:
        await safe_edit_or_send(callback, "❌ اطلاعات انتقال یافت نشد.")
        return

    sender = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if sender is None:
        await safe_edit_or_send(callback, "❌ حساب شما یافت نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.xui_client))
        .where(
            Subscription.id == UUID(sub_id_str),
            Subscription.user_id == sender.id,
            Subscription.status.in_(["active", "pending_activation"]),
        )
    )
    if sub is None:
        await safe_edit_or_send(callback, "❌ سرویس معتبر برای انتقال یافت نشد.")
        return

    # Transfer ownership
    recipient_uuid = UUID(recipient_user_id_str)
    sub.user_id = recipient_uuid

    # Update XUI record ownership too
    if sub.xui_client:
        sub.xui_client.user_id = recipient_uuid

    await session.flush()

    await safe_edit_or_send(
        callback,
        f"✅ <b>انتقال موفق!</b>\n\n"
        f"کانفیگ به کاربر <b>{recipient_name}</b> منتقل شد.",
    )

    # Notify recipient
    try:
        bot = callback.bot
        sender_display = f"@{sender.username}" if sender.username else f"ID: {sender.telegram_id}"
        await bot.send_message(
            recipient_telegram_id,
            f"🎁 <b>کانفیگ جدید دریافت کردید!</b>\n\n"
            f"👤 فرستنده: {sender_display}\n\n"
            "از بخش «📋 سرویس‌های من» می‌توانید کانفیگ را مشاهده کنید.",
        )
    except Exception as exc:
        logger.warning("Failed to notify transfer recipient: %s", exc)

    # Notify admins
    try:
        from services.notifications import notify_admins
        admin_text = (
            "🔄 <b>انتقال کانفیگ</b>\n\n"
            f"👤 فرستنده: {sender.first_name or '-'} (ID: <code>{sender.telegram_id}</code>)\n"
            f"👤 گیرنده: {recipient_name} (ID: <code>{recipient_telegram_id}</code>)\n"
            f"📦 سرویس: <code>{sub_id_str[:8]}...</code>"
        )
        await notify_admins(session, callback.bot, admin_text)
    except Exception as exc:
        logger.warning("Failed to notify admins about transfer: %s", exc)
