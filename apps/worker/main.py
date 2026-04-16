from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apps.worker.jobs.broadcast import process_broadcast_queue
from apps.worker.jobs.payments import sync_pending_payments
from apps.worker.jobs.retargeting import process_retargeting_campaigns
from apps.worker.jobs.subscriptions import sync_all_subscription_states
from apps.worker.jobs.expiry_notifications import send_expiry_notifications
from apps.worker.jobs.server_health import check_server_health
from apps.worker.jobs.backup import run_backup
from core.config import settings
from core.database import AsyncSessionFactory


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token.get_secret_value())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_pending_payments, "interval", minutes=3)
    scheduler.add_job(sync_all_subscription_states, "interval", minutes=10, max_instances=1, coalesce=True)
    scheduler.add_job(
        run_broadcast_queue,
        "interval",
        seconds=20,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_retargeting_campaigns,
        "cron",
        hour=10,
        minute=0,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_expiry_notifications,
        "cron",
        hour="*/6",
        minute=30,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_server_health_check,
        "cron",
        hour="*/4",
        minute=0,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_backup_job,
        "cron",
        hour="*/6",
        minute=15,
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


async def run_retargeting_campaigns(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        await process_retargeting_campaigns(session, bot)
        await session.commit()


async def run_expiry_notifications(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        await send_expiry_notifications(session, bot)
        await session.commit()


async def run_server_health_check(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        await check_server_health(session, bot)
        await session.commit()


async def run_backup_job(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        await run_backup(session, bot)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
