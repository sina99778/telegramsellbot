from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from core.texts import AdminButtons, AdminMessages
from repositories.admin import AdminStatsRepository
from repositories.settings import AppSettingsRepository
from apps.bot.utils.messaging import safe_edit_or_send
from core.formatting import format_volume_bytes

router = Router(name="admin-stats")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


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
    total_used_volume = format_volume_bytes(await stats_repository.get_total_used_bytes())
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.RESET_REVENUE, callback_data="admin:stats:reset_confirm")
    builder.button(text="📊 ظرفیت سرورها", callback_data="admin:stats:server_capacity")
    builder.button(text="❌ سرویس‌های منقضی", callback_data="admin:stats:expired_subs")
    builder.button(text="📥 خروجی CSV کاربران", callback_data="admin:stats:export_csv")
    builder.button(text="📅 گزارش فروش هفتگی", callback_data="admin:stats:weekly_sales")
    builder.button(text="🔄 آپدیت آنی مصرف", callback_data="admin:stats:force_sync")
    builder.button(text="💰 گزارش مالی", callback_data="admin:stats:financial")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    from datetime import datetime
    current_time = datetime.utcnow().strftime("%H:%M:%S (UTC)")
    
    text = AdminMessages.STATS_DASHBOARD.format(
        total_users=total_users,
        total_active_subscriptions=total_active_subscriptions,
        total_revenue=total_revenue,
        total_used_volume=total_used_volume,
        total_active_servers=total_active_servers,
    ) + f"\n\n🕒 به‌روزرسانی: {current_time}"

    await safe_edit_or_send(
        callback=callback,
        text=text,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:stats:force_sync")
async def admin_force_sync_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    """Force immediately syncing usage across all servers."""
    await callback.answer("⏳ در حال دریافت لحظه‌ای مصرف از سرورها... (ممکن است کمی طول بکشد)")
    from apps.worker.jobs.subscriptions import sync_all_subscription_states
    await sync_all_subscription_states()
    # Let users see the update
    # Call the dashboard again
    # We must commit current session so we read the updated data done by the worker's own session
    await session.commit()
    await admin_stats_dashboard(callback, session)


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

    await callback.message.answer_document(
        document=doc,
        caption="📥 دیتابیس کاربران"
    )


@router.callback_query(F.data == "admin:stats:weekly_sales")
async def admin_export_weekly_sales_csv(callback: CallbackQuery, session: AsyncSession) -> None:
    """Export weekly sales CSV."""
    await callback.answer("⏳ در حال تولید گزارش...")
    
    from sqlalchemy import select, desc
    from datetime import datetime, timedelta
    from models.order import Order
    from models.payment import Payment
    from models.subscription import Subscription
    from sqlalchemy.orm import selectinload
    import io
    import csv
    from aiogram.types import BufferedInputFile
    
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    
    stmt = (
        select(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.plan),
            selectinload(Order.subscription).selectinload(Subscription.xui_client),
        )
        .where(Order.created_at >= one_week_ago, Order.status.in_(["paid", "provisioned", "completed"]))
        .order_by(desc(Order.created_at))
    )
    result = await session.scalars(stmt)
    orders = result.all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order ID", "Date", "User ID", "Name", "Username", "Plan", "Config Name",
        "Amount Paid", "Currency", "Payment Method"
    ])
    
    for order in orders:
        user = order.user
        plan = order.plan
        config_name = ""
        if order.subscription is not None and order.subscription.xui_client is not None:
            config_name = order.subscription.xui_client.username
        if not config_name:
            payment = await session.scalar(
                select(Payment)
                .where(Payment.user_id == order.user_id)
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
            if payment is not None and isinstance(payment.callback_payload, dict):
                config_name = str(payment.callback_payload.get("config_name") or "")
        
        provider_mapped = "درگاه (Gateway)" if order.source == "gateway" else "موجودی کیف پول (Wallet)"
        
        writer.writerow([
            str(order.id),
            order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else "",
            str(user.telegram_id) if user else "",
            user.first_name if user else "",
            user.username if (user and user.username) else "",
            plan.name if plan else "Unknown Plan",
            config_name,
            f"{order.amount:.2f}",
            order.currency,
            provider_mapped
        ])
        
    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM for Excel
    doc = BufferedInputFile(csv_bytes, filename=f"weekly_sales_{datetime.utcnow().strftime('%Y%m%d')}.csv")
    
    await callback.message.answer_document(
        document=doc,
        caption="📅 گزارش فروش ۷ روز گذشته\n"
        f"تعداد سفارش: {len(orders)}\n\n"
        "💡 فایل پیوست را در اکسل باز کنید."
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


@router.callback_query(F.data == "admin:stats:financial")
async def admin_financial_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()

    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, func, case
    from models.payment import Payment
    from models.wallet import WalletTransaction
    from models.subscription import Subscription

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Today's revenue (successful payments)
    today_revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.price_amount), 0))
        .where(
            Payment.actually_paid.isnot(None),
            Payment.created_at >= today_start,
        )
    ) or 0

    # This week's revenue
    week_revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.price_amount), 0))
        .where(
            Payment.actually_paid.isnot(None),
            Payment.created_at >= week_ago,
        )
    ) or 0

    # This month's revenue
    month_revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.price_amount), 0))
        .where(
            Payment.actually_paid.isnot(None),
            Payment.created_at >= month_ago,
        )
    ) or 0

    # Wallet charges today
    wallet_today = await session.scalar(
        select(func.coalesce(func.sum(WalletTransaction.amount), 0))
        .where(
            WalletTransaction.direction == "credit",
            WalletTransaction.type == "topup",
            WalletTransaction.created_at >= today_start,
        )
    ) or 0

    # Payment status breakdown (last 30 days)
    status_counts = dict(
        (await session.execute(
            select(Payment.payment_status, func.count())
            .where(Payment.created_at >= month_ago)
            .group_by(Payment.payment_status)
        )).all()
    )

    # Active subscriptions
    active_subs = await session.scalar(
        select(func.count()).select_from(Subscription)
        .where(Subscription.status == "active")
    ) or 0

    # Stuck payments
    from sqlalchemy import or_
    stuck = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.actually_paid.isnot(None),
            Payment.kind == "direct_purchase",
            or_(
                ~Payment.callback_payload.has_key("provisioned"),
                Payment.callback_payload["provisioned"].as_boolean().is_(False),
            ),
        )
    ) or 0

    text = (
        "💰 گزارش مالی\n\n"
        f"📅 درآمد امروز: ${today_revenue:.2f}\n"
        f"📅 درآمد هفتگی: ${week_revenue:.2f}\n"
        f"📅 درآمد ماهانه: ${month_revenue:.2f}\n\n"
        f"💳 شارژ کیف پول امروز: ${wallet_today:.2f}\n"
        f"🔗 اشتراک فعال: {active_subs}\n"
        f"⚠️ پرداخت بدون تحویل: {stuck}\n\n"
        "📊 وضعیت پرداخت‌ها (۳۰ روز):\n"
    )
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        icon = {"finished": "✅", "confirmed": "✅", "failed": "❌", "expired": "⏰", "waiting": "⏳", "refunded": "💸"}.get(status, "❓")
        text += f"  {icon} {status}: {count}\n"

    builder = InlineKeyboardBuilder()
    if stuck > 0:
        builder.button(text=f"بررسی {stuck} پرداخت بدون تحویل", callback_data="admin:recovery")
    builder.button(text=AdminButtons.BACK, callback_data="admin:stats")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())
