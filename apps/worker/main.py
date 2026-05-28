from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler


HEARTBEAT_PATH = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker_heartbeat"))


async def _heartbeat_loop() -> None:
    while True:
        try:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_PATH.write_text(str(time.time()))
        except OSError as exc:
            logging.getLogger(__name__).warning("worker heartbeat write failed: %s", exc)
        await asyncio.sleep(30)

from apps.bot.premium_bot import PremiumEmojiBot
from apps.worker.jobs.broadcast import process_broadcast_queue
from apps.worker.jobs.payments import sync_pending_payments
from apps.worker.jobs.retargeting import process_retargeting_campaigns
from apps.worker.jobs.subscriptions import sync_all_subscription_states
from apps.worker.jobs.expiry_notifications import send_expiry_notifications
from apps.worker.jobs.server_health import check_server_health
from apps.worker.jobs.backup import run_backup
from apps.worker.jobs.card_autoconfirm import run_card_autoconfirm
from apps.worker.jobs.crypto_autoconfirm import run_crypto_autoconfirm
from apps.worker.jobs.reconciliation import run_reconciliation
from core.config import settings
from core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Bootstrap heartbeat — see apps/bot/main.py for rationale.
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(str(time.time()))
    except OSError as exc:
        logging.getLogger(__name__).warning("bootstrap heartbeat write failed: %s", exc)

    bot = PremiumEmojiBot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(sync_pending_payments, "interval", minutes=3)
    scheduler.add_job(run_sync_subscriptions, "interval", minutes=1, max_instances=1, coalesce=True)
    # Auto-confirm manual crypto topups by polling the blockchain.
    scheduler.add_job(
        run_crypto_autoconfirm_job,
        "interval",
        seconds=30,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
    # Auto-approve card-receipt payments older than the operator-configured
    # delay. Disabled by default; turned on from the bot admin / dashboard.
    scheduler.add_job(
        run_card_autoconfirm_job,
        "interval",
        seconds=60,
        kwargs={"bot": bot},
        max_instances=1,
        coalesce=True,
    )
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
    # Backup tick fires every 30 min. The job itself checks the operator-
    # configured `system.backup_interval_hours` setting and only actually
    # produces a backup if enough time has elapsed since the last one —
    # so operators can change the cadence from the dashboard without
    # restarting the worker.
    scheduler.add_job(
        run_backup_job,
        "cron",
        minute="*/30",
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

    heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="worker-heartbeat")
    try:
        await asyncio.Event().wait()
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
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


async def run_crypto_autoconfirm_job(bot: PremiumEmojiBot) -> None:
    """Poll TronGrid / toncenter for incoming deposits and confirm any
    pending manual-crypto invoices whose amount + timestamp matches."""
    try:
        async with AsyncSessionFactory() as session:
            result = await run_crypto_autoconfirm(session, bot)
            await session.commit()
            if result.get("confirmed"):
                logger.info("[CRYPTO-AUTOCONFIRM] %s", result)
    except Exception as exc:
        logger.error("crypto_autoconfirm_job failed: %s", exc, exc_info=True)


async def run_card_autoconfirm_job(bot: PremiumEmojiBot) -> None:
    """Auto-approve manual card-to-card receipts older than the
    operator-configured delay. No-ops when disabled."""
    try:
        async with AsyncSessionFactory() as session:
            result = await run_card_autoconfirm(session, bot)
            await session.commit()
            if result.get("confirmed"):
                logger.info("[CARD-AUTOCONFIRM] %s", result)
    except Exception as exc:
        logger.error("card_autoconfirm_job failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
