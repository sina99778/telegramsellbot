from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Share broadcast's bot-wide token bucket: both jobs send through the same
# bot token, so they must draw from the same ~30 msg/s Telegram budget.
from apps.worker.jobs.broadcast import _global_rate_gate
from core.database import utcnow
from models.order import Order
from models.user import User
from repositories.settings import AppSettingsRepository

logger = logging.getLogger(__name__)

RETARGETING_CONCURRENCY = 20


async def process_retargeting_campaigns(session: AsyncSession, bot: Bot) -> None:
    settings = await AppSettingsRepository(session).get_retargeting_settings()
    if not settings.enabled:
        return

    cutoff = utcnow() - timedelta(days=settings.days)
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
            await _global_rate_gate()
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=settings.message,
                )
            except TelegramRetryAfter as exc:
                # One user's flood-wait must not abort the whole campaign:
                # sleep out the penalty, retry once, then move on regardless.
                logger.warning("Retargeting hit FloodWait; sleeping %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await _global_rate_gate()
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=settings.message,
                    )
                except TelegramForbiddenError:
                    user.is_bot_blocked = True
                except Exception:
                    logger.warning(
                        "Retargeting send failed for user %s", user.telegram_id, exc_info=True
                    )
            except TelegramForbiddenError:
                user.is_bot_blocked = True
            except Exception:
                # Isolate per-user failures so the rest of the audience and the
                # is_bot_blocked updates collected so far are not discarded.
                logger.warning(
                    "Retargeting send failed for user %s", user.telegram_id, exc_info=True
                )

    # return_exceptions=True is a safety net: a stray exception from one task
    # must never abort the gather and skip the flush/commit of block flags.
    await asyncio.gather(*(send_one(user) for user in eligible_users), return_exceptions=True)
    await session.flush()
