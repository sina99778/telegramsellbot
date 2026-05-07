from __future__ import annotations

import asyncio
import logging

from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apps.bot.premium_bot import PremiumEmojiBot
from apps.worker.jobs.broadcast import process_broadcast_queue
from apps.worker.jobs.payments import sync_pending_payments
from apps.worker.jobs.retargeting import process_retargeting_campaigns
from apps.worker.jobs.subscriptions import sync_all_subscription_states
from apps.worker.jobs.expiry_notifications import send_expiry_notifications
from apps.worker.jobs.server_health import check_server_health
from apps.worker.jobs.backup import run_backup
from apps.worker.jobs.reconciliation import run_reconciliation
from core.config import settings
from core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = PremiumEmojiBot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_pending_payments, "interval", minutes=3)
    scheduler.add_job(run_sync_subscriptions, "interval", minutes=1, max_instances=1, coalesce=True)
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
        hour="*/3",
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
    scheduler.add_job(
        run_reconciliation_job,
        "cron",
        hour="*/3",
        minute=45,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    try:
        await asyncio.Event().wait()
    finally:
        await bot.session.close()


# ── Error-isolated job wrappers ──────────────────────────────────────────────


async def run_sync_subscriptions() -> None:
    try:
        await sync_all_subscription_states()
    except Exception as exc:
        logger.error("sync_all_subscription_states failed: %s", exc, exc_info=True)


async def run_broadcast_queue(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await process_broadcast_queue(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("broadcast_queue failed: %s", exc, exc_info=True)


async def run_retargeting_campaigns(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await process_retargeting_campaigns(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("retargeting_campaigns failed: %s", exc, exc_info=True)


async def run_expiry_notifications(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await send_expiry_notifications(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("expiry_notifications failed: %s", exc, exc_info=True)


async def run_server_health_check(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await check_server_health(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("server_health_check failed: %s", exc, exc_info=True)


async def run_backup_job(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await run_backup(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("backup_job failed: %s", exc, exc_info=True)


async def run_reconciliation_job(bot: PremiumEmojiBot) -> None:
    try:
        async with AsyncSessionFactory() as session:
            await run_reconciliation(session, bot)
            await session.commit()
    except Exception as exc:
        logger.error("reconciliation_job failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
