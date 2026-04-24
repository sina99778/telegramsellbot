"""
Admin Customer Management Handler.
View users with successful purchases and manage their configs.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from core.formatting import format_volume_bytes
from models.order import Order
from models.subscription import Subscription
from models.user import User

logger = logging.getLogger(__name__)

router = Router(name="admin-customers")
router.callback_query.middleware(AdminOnlyMiddleware())

PAGE_SIZE = 8


class CustPageCallback(CallbackData, prefix="acust"):
    page: int


class CustDetailCallback(CallbackData, prefix="acustd"):
    user_id: UUID


class CustSubCallback(CallbackData, prefix="acusts"):
    sub_id: UUID


@router.callback_query(F.data == "admin:customers")
async def customers_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show paginated list of users with successful orders."""
    await callback.answer()
    await _show_customers_page(callback, session, page=0)


@router.callback_query(CustPageCallback.filter())
async def customers_paginate(
    callback: CallbackQuery,
    callback_data: CustPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _show_customers_page(callback, session, page=callback_data.page)


async def _show_customers_page(callback: CallbackQuery, session: AsyncSession, page: int) -> None:
    """Display a page of customers who have at least one successful order."""
    # Get user IDs with successful orders
    subq = (
        select(distinct(Order.user_id))
        .where(Order.status.in_(["completed", "provisioned", "paid"]))
        .subquery()
    )

    # Count total
    total = await session.scalar(
        select(func.count()).select_from(subq)
    ) or 0

    if total == 0:
        await safe_edit_or_send(callback, "📭 هیچ مشتری با خرید موفق یافت نشد.")
        return

    # Get paginated users
    user_ids_result = await session.execute(
        select(subq.c[0]).limit(PAGE_SIZE).offset(page * PAGE_SIZE)
    )
    user_ids = [row[0] for row in user_ids_result.all()]

    users = []
    for uid in user_ids:
        u = await session.get(User, uid)
        if u:
            # Count orders
            order_count = await session.scalar(
                select(func.count()).select_from(Order)
                .where(Order.user_id == uid, Order.status.in_(["completed", "provisioned", "paid"]))
            ) or 0
            users.append((u, order_count))

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    text = (
        f"👥 <b>مدیریت مشتریان</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 تعداد کل: {total} مشتری\n"
        f"📄 صفحه {page + 1} از {total_pages}\n\n"
    )

    builder = InlineKeyboardBuilder()
    for u, oc in users:
        name = u.first_name or u.username or str(u.telegram_id)
        label = f"👤 {name} ({oc} خرید)"
        builder.button(text=label, callback_data=CustDetailCallback(user_id=u.id).pack())

    # Pagination
    nav_row = []
    if page > 0:
        nav_row.append(("◀️ قبلی", CustPageCallback(page=page - 1).pack()))
    if page < total_pages - 1:
        nav_row.append(("بعدی ▶️", CustPageCallback(page=page + 1).pack()))

    builder.adjust(1)
    for label, cbd in nav_row:
        builder.button(text=label, callback_data=cbd)
    if nav_row:
        builder.adjust(*([1] * len(users)), len(nav_row))

    builder.button(text="🔙 بازگشت", callback_data="admin:panel")

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(CustDetailCallback.filter())
async def customer_detail(
    callback: CallbackQuery,
    callback_data: CustDetailCallback,
    session: AsyncSession,
) -> None:
    """Show details and configs of a specific customer."""
    await callback.answer()

    user = await session.get(User, callback_data.user_id)
    if user is None:
        await safe_edit_or_send(callback, "❌ کاربر یافت نشد.")
        return

    # Get subscriptions
    subs_result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    )
    subs = list(subs_result.scalars().all())

    # Get order stats
    order_count = await session.scalar(
        select(func.count()).select_from(Order)
        .where(Order.user_id == user.id, Order.status.in_(["completed", "provisioned", "paid"]))
    ) or 0

    total_spent = await session.scalar(
        select(func.sum(Order.amount)).select_from(Order)
        .where(Order.user_id == user.id, Order.status.in_(["completed", "provisioned", "paid"]))
    ) or 0

    user_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.telegram_id}'>پروفایل</a>"
    status_emoji = {"active": "🟢", "pending_activation": "🟡", "expired": "🔴"}.get(user.status, "⚪")

    text = (
        f"👤 <b>اطلاعات مشتری</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 نام: {user.first_name or '-'}\n"
        f"🔗 لینک: {user_link}\n"
        f"🆔 تلگرام: <code>{user.telegram_id}</code>\n"
        f"📊 تعداد خرید: {order_count}\n"
        f"💰 مجموع خرید: {total_spent:.2f} USD\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>کانفیگ‌ها ({len(subs)}):</b>\n\n"
    )

    for sub in subs[:10]:  # Max 10
        st = {"active": "🟢", "pending_activation": "🟡", "expired": "🔴"}.get(sub.status, "⚪")
        plan_name = sub.plan.name if sub.plan else "?"
        used = format_volume_bytes(sub.used_bytes)
        total = format_volume_bytes(sub.volume_bytes)
        text += f"{st} {plan_name} — {used}/{total}\n"

    builder = InlineKeyboardBuilder()
    for sub in subs[:8]:
        plan_name = sub.plan.name if sub.plan else "?"
        st = {"active": "🟢", "pending_activation": "🟡", "expired": "🔴"}.get(sub.status, "⚪")
        builder.button(
            text=f"{st} {plan_name}",
            callback_data=CustSubCallback(sub_id=sub.id).pack(),
        )
    builder.button(text="🔙 بازگشت", callback_data="admin:customers")
    builder.adjust(2)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(CustSubCallback.filter())
async def customer_sub_detail(
    callback: CallbackQuery,
    callback_data: CustSubCallback,
    session: AsyncSession,
) -> None:
    """Show full details of a customer's subscription."""
    await callback.answer()

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.user),
            selectinload(Subscription.xui_client),
        )
        .where(Subscription.id == callback_data.sub_id)
    )
    if sub is None:
        await safe_edit_or_send(callback, "❌ سرویس یافت نشد.")
        return

    st = {"active": "🟢 فعال", "pending_activation": "🟡 در انتظار", "expired": "🔴 منقضی"}.get(sub.status, sub.status)
    plan_name = sub.plan.name if sub.plan else "?"
    used = format_volume_bytes(sub.used_bytes)
    total = format_volume_bytes(sub.volume_bytes)

    text = (
        f"📦 <b>جزئیات سرویس</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 پلن: {plan_name}\n"
        f"📊 وضعیت: {st}\n"
        f"💾 مصرف: {used} / {total}\n"
    )

    if sub.starts_at:
        text += f"📅 شروع: {sub.starts_at.strftime('%Y-%m-%d')}\n"
    if sub.ends_at:
        text += f"📅 پایان: {sub.ends_at.strftime('%Y-%m-%d')}\n"
    if sub.sub_link:
        text += f"\n🔗 لینک:\n<code>{sub.sub_link}</code>\n"

    builder = InlineKeyboardBuilder()
    if sub.user:
        builder.button(text="🔙 بازگشت", callback_data=CustDetailCallback(user_id=sub.user.id).pack())

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())
