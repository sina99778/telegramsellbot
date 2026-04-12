from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from repositories.admin import AdminStatsRepository


router = Router(name="admin-stats")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:stats")
async def admin_stats_dashboard(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()

    stats_repository = AdminStatsRepository(session)
    total_users = await stats_repository.get_total_users()
    total_active_subscriptions = await stats_repository.get_total_active_subscriptions()
    total_revenue = await stats_repository.get_total_revenue()
    total_active_servers = await stats_repository.get_total_active_servers()

    await callback.message.answer(
        (
            "Statistics Dashboard\n\n"
            f"Total Users: {total_users}\n"
            f"Active Subscriptions: {total_active_subscriptions}\n"
            f"Total Revenue: {total_revenue} USD\n"
            f"Active Servers: {total_active_servers}"
        )
    )
