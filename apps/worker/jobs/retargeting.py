from __future__ import annotations

import asyncio
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import utcnow
from core.texts import MarketingTexts
from models.order import Order
from models.user import User


RETARGETING_DAYS = 30
RETARGETING_CONCURRENCY = 20


async def process_retargeting_campaigns(session: AsyncSession, bot: Bot) -> None:
    cutoff = utcnow() - timedelta(days=RETARGETING_DAYS)
    latest_order_subquery = (
        select(Order.user_id, func.max(Order.created_at).label("last_order_at"))
        .group_by(Order.user_id)
        .subquery()
    )

    result = await session.execute(
        select(User)
        .options(selectinload(User.subscriptions))
        .outerjoin(latest_order_subquery, latest_order_subquery.c.user_id == User.id)
        .where(
            (latest_order_subquery.c.last_order_at.is_(None)) | (latest_order_subquery.c.last_order_at <= cutoff),
            User.status == "active",
        )
    )
    users = list(result.scalars().unique().all())
    eligible_users = [user for user in users if not any(sub.status == "active" for sub in user.subscriptions)]
    semaphore = asyncio.Semaphore(RETARGETING_CONCURRENCY)

    async def send_one(user: User) -> None:
        async with semaphore:
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=MarketingTexts.RETARGETING_REMINDER,
                )
            except TelegramForbiddenError:
                user.is_bot_blocked = True

    await asyncio.gather(*(send_one(user) for user in eligible_users))
    await session.flush()
