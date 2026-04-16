from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from core.texts import AdminButtons, AdminMessages
from repositories.admin import AdminStatsRepository


router = Router(name="admin-stats")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


from repositories.settings import AppSettingsRepository
from apps.bot.utils.messaging import safe_edit_or_send

@router.callback_query(F.data == "admin:stats")
async def admin_stats_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()

    stats_repository = AdminStatsRepository(session)
    settings_repository = AppSettingsRepository(session)
    
    reset_at = await settings_repository.get_revenue_reset_at()
    
    total_users = await stats_repository.get_total_users()
    total_active_subscriptions = await stats_repository.get_total_active_subscriptions()
    total_revenue = await stats_repository.get_total_revenue(reset_at=reset_at)
    total_active_servers = await stats_repository.get_total_active_servers()

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.RESET_REVENUE, callback_data="admin:stats:reset_confirm")
    builder.button(text="📊 ظرفیت سرورها", callback_data="admin:stats:server_capacity")
    builder.button(text="❌ سرویس‌های منقضی", callback_data="admin:stats:expired_subs")
    builder.button(text="📥 خروجی CSV کاربران", callback_data="admin:stats:export_csv")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    await callback.message.edit_text(
        AdminMessages.STATS_DASHBOARD.format(
            total_users=total_users,
            total_active_subscriptions=total_active_subscriptions,
            total_revenue=total_revenue,
            total_active_servers=total_active_servers,
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:stats:server_capacity")
async def admin_server_capacity(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show server capacity report."""
    await callback.answer()
    from sqlalchemy import select, func
    from models.xui import XUIServerRecord, XUIInboundRecord, XUIClientRecord

    result = await session.execute(
        select(XUIServerRecord).where(XUIServerRecord.is_active.is_(True))
    )
    servers = list(result.scalars().all())

    if not servers:
        await safe_edit_or_send(callback, "هیچ سرور فعالی وجود ندارد.")
        return

    lines = ["📊 گزارش ظرفیت سرورها:\n"]
    for server in servers:
        active_clients = await session.scalar(
            select(func.count())
            .select_from(XUIClientRecord)
            .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
            .where(
                XUIClientRecord.is_active.is_(True),
                XUIInboundRecord.server_id == server.id,
            )
        ) or 0

        max_label = str(server.max_clients) if server.max_clients else "∞"
        pct = ""
        if server.max_clients and server.max_clients > 0:
            ratio = round((active_clients / server.max_clients) * 100)
            pct = f" ({ratio}%)"

        name = server.name or server.base_url
        lines.append(f"🖥 {name}: {active_clients}/{max_label}{pct}")

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:stats")
    builder.adjust(1)
    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:stats:expired_subs")
async def admin_expired_subs(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show recent expired subscriptions."""
    await callback.answer()
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from models.subscription import Subscription
    from models.user import User

    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.plan))
        .where(Subscription.status == "expired")
        .order_by(Subscription.expired_at.desc())
        .limit(20)
    )
    subs = list(result.scalars().all())

    if not subs:
        await safe_edit_or_send(callback, "هیچ سرویس منقضی‌ای وجود ندارد.")
        return

    lines = ["❌ آخرین ۲۰ سرویس منقضی:\n"]
    for sub in subs:
        user_name = sub.user.first_name if sub.user else "-"
        plan_name = sub.plan.name if sub.plan else "-"
        expired = sub.expired_at.strftime("%Y-%m-%d") if sub.expired_at else "-"
        lines.append(f"👤 {user_name} | 📦 {plan_name} | 📅 {expired}")

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:stats")
    builder.adjust(1)
    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:stats:export_csv")
async def admin_export_csv(callback: CallbackQuery, session: AsyncSession) -> None:
    """Export all users as CSV file."""
    await callback.answer()
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from models.user import User
    from models.wallet import Wallet
    from aiogram.types import BufferedInputFile
    import csv
    import io

    result = await session.execute(
        select(User).options(selectinload(User.wallet)).order_by(User.created_at.desc())
    )
    users = list(result.scalars().all())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["telegram_id", "first_name", "username", "status", "balance", "created_at"])

    for user in users:
        balance = f"{user.wallet.balance:.2f}" if user.wallet else "0.00"
        writer.writerow([
            user.telegram_id,
            user.first_name or "",
            user.username or "",
            user.status,
            balance,
            user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility
    doc = BufferedInputFile(csv_bytes, filename="users_export.csv")

    bot = callback.bot
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=doc,
        caption=f"📥 خروجی {len(users)} کاربر",
    )


@router.callback_query(F.data == "admin:stats:reset_confirm")
async def admin_stats_reset_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ بله، صفر کن", callback_data="admin:stats:reset_now")
    builder.button(text=AdminButtons.BACK, callback_data="admin:stats")
    builder.adjust(1)
    
    await callback.message.edit_text(
        AdminMessages.CONFIRM_RESET_REVENUE,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:stats:reset_now")
async def admin_stats_reset_now(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    
    settings_repository = AppSettingsRepository(session)
    await settings_repository.reset_revenue()
    
    await safe_edit_or_send(callback, AdminMessages.REVENUE_RESET_SUCCESS)
    await admin_stats_dashboard(callback, session)
