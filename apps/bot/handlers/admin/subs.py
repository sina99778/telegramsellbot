"""
Admin Subscription Management.

Provides:
- User subscription list (paginated)
- Per-subscription detail view
- Extend days / add volume
- Enable / disable
- Resend config
- Revoke (delete from X-UI)
- Zero-usage refund
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from core.formatting import format_volume_bytes, format_usage_bar
from core.texts import AdminButtons, AdminMessages
from models.order import Order
from models.subscription import Subscription
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.audit import AuditLogRepository
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded

logger = logging.getLogger(__name__)

router = Router(name="admin-subs")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

SUB_PAGE_SIZE = 5


# ─── FSM States ───────────────────────────────────────────────────────────────


class SubManageStates(StatesGroup):
    waiting_for_extend_days = State()
    waiting_for_add_volume_gb = State()


# ─── Callback Data ────────────────────────────────────────────────────────────


class AdminSubscriptionActionCallback(CallbackData, prefix="admin_sub"):
    action: str  # detail, revoke, toggle, extend, add_vol, resend, refund
    subscription_id: UUID
    user_id: UUID
    page: int = 1


class AdminSubscriptionListPageCallback(CallbackData, prefix="admin_sub_list"):
    user_id: UUID
    page: int = 1


# ─── Subscription List ────────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionListPageCallback.filter())
async def view_user_configs_page(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=callback_data.page,
    )


async def _render_user_configs(
    *,
    callback: CallbackQuery,
    session: AsyncSession,
    user_id: UUID,
    page: int,
) -> None:
    user = await session.scalar(
        select(User)
        .options(
            selectinload(User.subscriptions)
            .selectinload(Subscription.plan),
        )
        .where(User.id == user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    all_subs = sorted(user.subscriptions, key=lambda s: s.created_at or datetime.min, reverse=True)
    if not all_subs:
        builder = InlineKeyboardBuilder()
        builder.button(text=AdminButtons.BACK, callback_data="admin:users")
        builder.adjust(1)
        await safe_edit_or_send(callback, "این کاربر هیچ اشتراکی ندارد.", reply_markup=builder.as_markup())
        return

    start = max(page - 1, 0) * SUB_PAGE_SIZE
    page_items = all_subs[start: start + SUB_PAGE_SIZE]

    lines = [f"📋 اشتراک‌های کاربر ({len(all_subs)} مورد)\n"]
    for sub in page_items:
        status_icon = {"active": "🟢", "pending_activation": "⏳", "expired": "🔴", "cancelled": "⛔"}.get(sub.status, "❓")
        plan_name = sub.plan.name if sub.plan else "-"
        usage = f"{format_volume_bytes(sub.used_bytes)}/{format_volume_bytes(sub.volume_bytes)}"
        ends = sub.ends_at.strftime("%Y-%m-%d") if sub.ends_at else "-"
        lines.append(
            f"{status_icon} {plan_name}\n"
            f"   مصرف: {usage} | انقضا: {ends}"
        )

    builder = InlineKeyboardBuilder()
    for sub in page_items:
        status_icon = {"active": "🟢", "pending_activation": "⏳", "expired": "🔴", "cancelled": "⛔"}.get(sub.status, "❓")
        plan_name = sub.plan.name[:12] if sub.plan else str(sub.id)[:8]
        builder.button(
            text=f"{status_icon} {plan_name} | {str(sub.id)[:8]}",
            callback_data=AdminSubscriptionActionCallback(
                action="detail",
                subscription_id=sub.id,
                user_id=user.id,
                page=page,
            ).pack(),
        )

    # Pagination
    add_pagination_controls(
        builder,
        page=page,
        total_items=len(all_subs),
        page_size=SUB_PAGE_SIZE,
        prev_callback_data=AdminSubscriptionListPageCallback(user_id=user.id, page=page - 1).pack(),
        next_callback_data=AdminSubscriptionListPageCallback(user_id=user.id, page=page + 1).pack(),
    )
    builder.button(text=AdminButtons.BACK, callback_data="admin:users")
    builder.adjust(1)

    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


# ─── Subscription Detail ─────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "detail"))
async def subscription_detail(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _render_sub_detail(callback, callback_data.subscription_id, callback_data.user_id, callback_data.page, session)


async def _render_sub_detail(
    callback: CallbackQuery,
    sub_id: UUID,
    user_id: UUID,
    page: int,
    session: AsyncSession,
) -> None:
    sub = await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.plan), selectinload(Subscription.xui_client))
        .where(Subscription.id == sub_id)
    )
    if sub is None:
        await safe_edit_or_send(callback, AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    status_icon = {"active": "🟢", "pending_activation": "⏳", "expired": "🔴", "cancelled": "⛔"}.get(sub.status, "❓")
    plan_name = sub.plan.name if sub.plan else "-"
    usage_bar = format_usage_bar(sub.used_bytes, sub.volume_bytes)

    text = (
        f"📦 جزئیات اشتراک\n\n"
        f"🆔 ID: <code>{sub.id}</code>\n"
        f"{status_icon} وضعیت: {sub.status}\n"
        f"📦 پلن: {plan_name}\n"
        f"📊 مصرف: {format_volume_bytes(sub.used_bytes)} / {format_volume_bytes(sub.volume_bytes)}\n"
        f"{usage_bar}\n"
        f"📅 شروع: {sub.starts_at.strftime('%Y-%m-%d %H:%M') if sub.starts_at else '-'}\n"
        f"📅 انقضا: {sub.ends_at.strftime('%Y-%m-%d %H:%M') if sub.ends_at else '-'}\n"
        f"📅 فعال‌سازی: {sub.activated_at.strftime('%Y-%m-%d %H:%M') if sub.activated_at else '-'}\n"
        f"🔗 لینک: {sub.sub_link[:50] + '...' if sub.sub_link and len(sub.sub_link) > 50 else sub.sub_link or '-'}\n"
        f"📡 آخرین sync: {sub.last_usage_sync_at.strftime('%m/%d %H:%M') if sub.last_usage_sync_at else '-'}\n"
    )

    builder = InlineKeyboardBuilder()
    cb = lambda action: AdminSubscriptionActionCallback(
        action=action, subscription_id=sub.id, user_id=user_id, page=page,
    ).pack()

    if sub.status in {"active", "pending_activation"}:
        builder.button(text="📅 تمدید (روز)", callback_data=cb("extend"))
        builder.button(text="📦 افزایش حجم (GB)", callback_data=cb("add_vol"))
        builder.button(text="⛔ غیرفعال کردن", callback_data=cb("toggle"))
        builder.button(text="🗑 لغو و حذف از سرور", callback_data=cb("revoke"))

        if sub.sub_link:
            builder.button(text="📨 ارسال مجدد کانفیگ", callback_data=cb("resend"))

        if sub.used_bytes == 0:
            builder.button(text="💸 بازپرداخت (مصرف صفر)", callback_data=cb("refund"))

    elif sub.status == "expired":
        builder.button(text="📅 تمدید (روز)", callback_data=cb("extend"))
        builder.button(text="🟢 فعال کردن مجدد", callback_data=cb("toggle"))

    elif sub.status == "cancelled":
        builder.button(text="🟢 فعال کردن مجدد", callback_data=cb("toggle"))

    builder.button(text=AdminButtons.BACK, callback_data=AdminSubscriptionListPageCallback(user_id=user_id, page=page).pack())
    builder.adjust(2, 2, 1, 1, 1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Toggle Enable/Disable ───────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "toggle"))
async def toggle_subscription(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()

    sub = await session.scalar(select(Subscription).where(Subscription.id == callback_data.subscription_id))
    if sub is None:
        await safe_edit_or_send(callback, AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    old_status = sub.status
    if sub.status in {"active", "pending_activation"}:
        sub.status = "cancelled"
        new_status = "cancelled"
    else:
        sub.status = "active"
        new_status = "active"
        if sub.ends_at and sub.ends_at < datetime.now(timezone.utc):
            sub.ends_at = datetime.now(timezone.utc) + timedelta(days=30)

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_subscription",
        entity_type="subscription",
        entity_id=sub.id,
        payload={"old_status": old_status, "new_status": new_status},
    )
    await session.flush()
    await _render_sub_detail(callback, sub.id, callback_data.user_id, callback_data.page, session)


# ─── Extend Days ──────────────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "extend"))
async def extend_prompt(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SubManageStates.waiting_for_extend_days)
    await state.update_data(
        sub_id=str(callback_data.subscription_id),
        user_id=str(callback_data.user_id),
        page=callback_data.page,
    )
    await safe_edit_or_send(callback, "📅 چند روز تمدید شود؟ عدد وارد کنید.\nبرای لغو /cancel بزنید.")


@router.message(SubManageStates.waiting_for_extend_days)
async def extend_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text or message.text.startswith("/"):
        await state.clear()
        return

    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد صحیح مثبت وارد کنید.")
        return

    data = await state.get_data()
    await state.clear()

    sub = await session.scalar(
        select(Subscription).options(selectinload(Subscription.plan))
        .where(Subscription.id == UUID(data["sub_id"]))
    )
    if sub is None:
        await message.answer(AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    now = datetime.now(timezone.utc)
    base = sub.ends_at if sub.ends_at and sub.ends_at > now else now
    sub.ends_at = base + timedelta(days=days)

    if sub.status in {"expired", "cancelled"}:
        sub.status = "active"

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="extend_subscription",
        entity_type="subscription",
        entity_id=sub.id,
        payload={"days": days, "new_ends_at": sub.ends_at.isoformat()},
    )

    await message.answer(
        f"✅ اشتراک {str(sub.id)[:8]} به مدت {days} روز تمدید شد.\n"
        f"انقضای جدید: {sub.ends_at.strftime('%Y-%m-%d %H:%M')}"
    )


# ─── Add Volume ───────────────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "add_vol"))
async def add_volume_prompt(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SubManageStates.waiting_for_add_volume_gb)
    await state.update_data(
        sub_id=str(callback_data.subscription_id),
        user_id=str(callback_data.user_id),
        page=callback_data.page,
    )
    await safe_edit_or_send(callback, "📦 چند گیگابایت اضافه شود؟ عدد وارد کنید.\nبرای لغو /cancel بزنید.")


@router.message(SubManageStates.waiting_for_add_volume_gb)
async def add_volume_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text or message.text.startswith("/"):
        await state.clear()
        return

    try:
        gb = float(message.text.strip())
        if gb <= 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد مثبت وارد کنید.")
        return

    data = await state.get_data()
    await state.clear()

    sub = await session.scalar(
        select(Subscription).where(Subscription.id == UUID(data["sub_id"]))
    )
    if sub is None:
        await message.answer(AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    added_bytes = int(gb * 1024**3)
    old_vol = sub.volume_bytes
    sub.volume_bytes += added_bytes

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="add_volume",
        entity_type="subscription",
        entity_id=sub.id,
        payload={"added_gb": gb, "old_bytes": old_vol, "new_bytes": sub.volume_bytes},
    )

    await message.answer(
        f"✅ {gb:.1f} GB به اشتراک {str(sub.id)[:8]} اضافه شد.\n"
        f"حجم جدید: {format_volume_bytes(sub.volume_bytes)}"
    )


# ─── Resend Config ────────────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "resend"))
async def resend_config(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
    admin_user: User,
    bot: Bot,
) -> None:
    await callback.answer("⏳ در حال ارسال...")

    sub = await session.scalar(
        select(Subscription).options(selectinload(Subscription.user))
        .where(Subscription.id == callback_data.subscription_id)
    )
    if sub is None or sub.user is None or not sub.sub_link:
        await safe_edit_or_send(callback, "اشتراک یا لینک یافت نشد.")
        return

    try:
        await bot.send_message(
            sub.user.telegram_id,
            f"📨 ارسال مجدد کانفیگ از سوی پشتیبانی:\n\n🔗 ساب لینک:\n{sub.sub_link}",
        )
        try:
            from core.qr import make_qr_bytes
            from aiogram.types import BufferedInputFile
            qr_bytes = make_qr_bytes(sub.sub_link)
            if qr_bytes:
                await bot.send_photo(
                    chat_id=sub.user.telegram_id,
                    photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
                    caption="📷 QR کد کانفیگ",
                )
        except Exception:
            pass

        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="resend_config",
            entity_type="subscription",
            entity_id=sub.id,
            payload={"user_telegram_id": sub.user.telegram_id},
        )
        await safe_edit_or_send(callback, f"✅ کانفیگ به کاربر ارسال شد.")
    except TelegramForbiddenError:
        await safe_edit_or_send(callback, "❌ کاربر ربات را بلاک کرده.")
    except Exception as exc:
        await safe_edit_or_send(callback, f"❌ خطا: {str(exc)[:200]}")


# ─── Zero-Usage Refund ────────────────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "refund"))
async def zero_usage_refund(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.order),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(Subscription.id == callback_data.subscription_id)
    )
    if sub is None:
        await safe_edit_or_send(callback, AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    if sub.used_bytes > 0:
        await safe_edit_or_send(callback, "❌ مصرف این اشتراک صفر نیست. بازپرداخت امکان‌پذیر نیست.")
        return

    order = sub.order
    if order is None:
        await safe_edit_or_send(callback, "❌ سفارش مرتبط یافت نشد.")
        return

    # Refund to wallet
    from services.wallet.manager import WalletManager
    try:
        wallet_manager = WalletManager(session)
        await wallet_manager.process_transaction(
            user_id=sub.user_id,
            amount=order.amount,
            transaction_type="admin_zero_usage_refund",
            direction="credit",
            currency=order.currency,
            reference_type="order",
            reference_id=order.id,
            description="Admin zero-usage refund",
            metadata={"admin_id": str(admin_user.id), "subscription_id": str(sub.id)},
        )

        order.status = "refunded"
        sub.status = "cancelled"
        sub.sub_link = None

        # Delete from X-UI
        xui_record = sub.xui_client
        if xui_record and xui_record.inbound:
            try:
                server = ensure_inbound_server_loaded(xui_record.inbound)
                async with create_xui_client_for_server(server) as xui_client:
                    await xui_client.delete_client(
                        inbound_id=xui_record.inbound.xui_inbound_remote_id,
                        client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                    )
                xui_record.is_active = False
            except Exception as exc:
                logger.warning("Could not delete X-UI client on refund: %s", exc)

        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="zero_usage_refund",
            entity_type="subscription",
            entity_id=sub.id,
            payload={"amount": str(order.amount), "order_id": str(order.id)},
        )

        await safe_edit_or_send(
            callback,
            f"✅ مبلغ {order.amount} {order.currency} به کیف پول بازگردانده شد.\n"
            f"اشتراک لغو و از سرور حذف شد."
        )
    except Exception as exc:
        logger.error("Admin zero-usage refund failed: %s", exc, exc_info=True)
        await safe_edit_or_send(callback, f"❌ خطا در بازپرداخت:\n{str(exc)[:300]}")


# ─── Revoke (Delete from X-UI) ───────────────────────────────────────────────


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "revoke"))
async def revoke_user_config(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    subscription = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials)
        )
        .where(Subscription.id == callback_data.subscription_id)
    )
    if subscription is None:
        await safe_edit_or_send(callback, AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    xui_record = subscription.xui_client
    if xui_record is not None and xui_record.inbound is not None:
        try:
            server = ensure_inbound_server_loaded(xui_record.inbound)
            async with create_xui_client_for_server(server) as xui_client:
                await xui_client.delete_client(
                    inbound_id=xui_record.inbound.xui_inbound_remote_id,
                    client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                )
        except Exception as exc:
            logger.error("Failed to delete X-UI client on admin revoke: %s", exc)
        xui_record.is_active = False

    subscription.status = "cancelled"
    subscription.sub_link = None
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="revoke_config",
        entity_type="subscription",
        entity_id=subscription.id,
        payload={"user_id": str(subscription.user_id), "status": "cancelled"},
    )
    await session.flush()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=callback_data.page,
    )
