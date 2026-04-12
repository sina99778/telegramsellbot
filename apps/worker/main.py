from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apps.worker.jobs.broadcast import process_broadcast_queue
from apps.worker.jobs.payments import sync_pending_payments
from apps.worker.jobs.retargeting import send_inactive_user_reminders
from apps.worker.jobs.subscriptions import expire_due_subscriptions, sync_first_use_activations
from core.config import settings
from core.database import AsyncSessionFactory


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token.get_secret_value())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_pending_payments, "interval", minutes=3)
    scheduler.add_job(sync_first_use_activations, "interval", minutes=5)
    scheduler.add_job(expire_due_subscriptions, "interval", minutes=10)
    scheduler.add_job(send_inactive_user_reminders, "cron", hour=10, minute=0)
    scheduler.add_job(
        run_broadcast_queue,
        "interval",
        seconds=20,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    try:
        await asyncio.Event().wait()
    finally:
        await bot.session.close()


async def run_broadcast_queue(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        await process_broadcast_queue(session, bot)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
