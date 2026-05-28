from __future__ import annotations

from uuid import UUID

from aiogram import Bot, F, Router
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
import asyncio

# Strong references for fire-and-forget background tasks. Without holding a
# reference the asyncio event loop is free to GC them mid-execution.
_BG_TASKS: set[asyncio.Task] = set()
from services.admin_gifts import (
    WALLET_GIFT_SEGMENTS,
    _wallet_gift_segment_label,
    grant_bulk_subscription_gift_background,
    grant_bulk_wallet_gift_background,
)


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


class WalletGiftSegmentCallback(CallbackData, prefix="wg_seg"):
    segment: str


@router.callback_query(F.data == "admin:gifts")
async def gift_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Top-level gift menu. Two flavours:
      * هدیه به کیف پول  → bulk wallet credit, segments by user state
      * هدیه به سرویس  → time/volume gift to existing subscriptions
    """
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 هدیه به کیف پول کاربران", callback_data="admin:gifts:wallet")
    builder.button(text="📅 هدیه زمان/حجم سرویس‌ها", callback_data="admin:gifts:subs")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "🎁 <b>هدیه گروهی</b>\n\nنوع هدیه را انتخاب کن:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:gifts:subs")
async def gift_subs_entry(callback: CallbackQuery, state: FSMContext) -> None:
    """Legacy entry-point — subscription gift flow."""
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="فقط کانفیگ‌های فعال", callback_data=GiftScopeCallback(status_scope="active").pack())
    builder.button(text="همه کانفیگ‌ها", callback_data=GiftScopeCallback(status_scope="all").pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:gifts")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "📅 هدیه گروهی به کانفیگ‌ها\n\nابتدا مشخص کنید هدیه به کدام کانفیگ‌ها اعمال شود.",
        reply_markup=builder.as_markup(),
    )


# ─── Wallet-credit bulk gift ────────────────────────────────────────────


@router.callback_query(F.data == "admin:gifts:wallet")
async def wallet_gift_entry(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    for seg in WALLET_GIFT_SEGMENTS:
        builder.button(text=_wallet_gift_segment_label(seg), callback_data=WalletGiftSegmentCallback(segment=seg).pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:gifts")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "💰 <b>شارژ کیف پول گروهی</b>\n\nمحدوده‌ی کاربران را انتخاب کن:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(WalletGiftSegmentCallback.filter())
async def wallet_gift_segment_chosen(
    callback: CallbackQuery,
    callback_data: WalletGiftSegmentCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(wallet_gift_segment=callback_data.segment)
    await state.set_state(BulkGiftStates.waiting_for_amount)
    await state.update_data(wallet_gift_phase="amount")
    await safe_edit_or_send(
        callback,
        "💰 مبلغ شارژ هر کاربر را به <b>دلار</b> بفرست (مثلاً <code>0.5</code> یا <code>5</code>).\n\n"
        "<i>اگر می‌خواهی یک یادداشت کوتاه روی هدیه قرار بدی، بعد از مبلغ یک خط جدید بزن و یادداشتت رو بنویس.</i>\n\n"
        "برای لغو /cancel را بفرست.",
        parse_mode="HTML",
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

    # Branch into the wallet-gift flow if the user came in via "💰 هدیه به کیف پول".
    if data.get("wallet_gift_phase") == "amount":
        await _wallet_gift_amount_entered(message, state, data)
        return

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
    bot: Bot,
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

    msg = await callback.message.edit_text("⏳ در حال آماده‌سازی و ارسال هدایا... لطفاً این پیام را پاک نکنید.")
    if isinstance(msg, bool):
        msg = callback.message
    
    task = asyncio.create_task(
        grant_bulk_subscription_gift_background(
            bot=bot,
            admin_telegram_id=callback.from_user.id,
            admin_user_id=admin_user.id,
            progress_message_id=msg.message_id,
            gift_type=gift_type,
            amount=amount,
            status_scope=status_scope,
            server_id=server_id,
        ),
        name=f"bulk-gift-{admin_user.id}",
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# ─── Wallet-credit bulk gift: amount + confirm + dispatch ──────────────


class WalletGiftConfirmCallback(CallbackData, prefix="wg_ok"):
    action: str  # "apply" | "cancel"


async def _wallet_gift_amount_entered(message: Message, state: FSMContext, data: dict) -> None:
    text = (message.text or "").strip()
    if text.lower() == "/cancel":
        await state.clear()
        await message.answer("لغو شد.")
        return
    # Split first-line = amount, rest = optional note.
    first, sep, rest = text.partition("\n")
    try:
        amount = float(first.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ عدد نامعتبر. مثلاً <code>0.5</code> یا <code>5</code> بفرست.", parse_mode="HTML")
        return

    note = rest.strip() or None
    segment = str(data.get("wallet_gift_segment") or "all")
    await state.update_data(
        wallet_gift_amount=amount,
        wallet_gift_note=note,
        wallet_gift_phase="confirm",
    )

    note_line = f"\nیادداشت: <i>{note}</i>" if note else ""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ ارسال هدیه", callback_data=WalletGiftConfirmCallback(action="apply").pack())
    builder.button(text="❌ لغو", callback_data=WalletGiftConfirmCallback(action="cancel").pack())
    builder.adjust(1)
    await message.answer(
        "🎁 <b>تأیید نهایی</b>\n\n"
        f"محدوده: <b>{_wallet_gift_segment_label(segment)}</b>\n"
        f"مبلغ هر نفر: <b>{amount} $</b>"
        f"{note_line}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(WalletGiftConfirmCallback.filter())
async def wallet_gift_confirm(
    callback: CallbackQuery,
    callback_data: WalletGiftConfirmCallback,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
    bot: Bot,
) -> None:
    await callback.answer()
    if callback_data.action != "apply":
        await state.clear()
        await safe_edit_or_send(callback, "لغو شد.")
        return

    data = await state.get_data()
    await state.clear()
    segment = str(data.get("wallet_gift_segment") or "all")
    amount = float(data.get("wallet_gift_amount") or 0)
    note = data.get("wallet_gift_note") or None

    if amount <= 0:
        await safe_edit_or_send(callback, "❌ مبلغ نامعتبر — لطفاً دوباره از منو شروع کن.")
        return

    msg = await callback.message.edit_text("⏳ در حال آماده‌سازی و ارسال هدایا... لطفاً این پیام را پاک نکنید.")
    if isinstance(msg, bool):
        msg = callback.message

    task = asyncio.create_task(
        grant_bulk_wallet_gift_background(
            bot=bot,
            admin_telegram_id=callback.from_user.id,
            admin_user_id=admin_user.id,
            progress_message_id=msg.message_id,
            segment=segment,
            amount_usd=amount,
            note=note,
        ),
        name=f"bulk-wallet-gift-{admin_user.id}",
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
