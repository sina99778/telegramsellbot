from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.broadcast import BroadcastJob
from models.user import User

logger = logging.getLogger(__name__)

# Telegram global limit is ~30 messages/sec across the bot. We stay well
# under that with a shared token bucket so multiple workers / overlapping
# job runs don't collectively breach the limit.
_GLOBAL_RATE_MAX = 25
_GLOBAL_RATE_WINDOW = 1.0  # seconds
_global_send_timestamps: list[float] = []
_global_lock = asyncio.Lock()


async def _global_rate_gate() -> None:
    async with _global_lock:
        now = time.monotonic()
        cutoff = now - _GLOBAL_RATE_WINDOW
        while _global_send_timestamps and _global_send_timestamps[0] < cutoff:
            _global_send_timestamps.pop(0)
        if len(_global_send_timestamps) >= _GLOBAL_RATE_MAX:
            sleep_for = _GLOBAL_RATE_WINDOW - (now - _global_send_timestamps[0]) + 0.01
            await asyncio.sleep(max(0.01, sleep_for))
        _global_send_timestamps.append(time.monotonic())


async def _send_broadcast_to_user(bot: Bot, job: BroadcastJob, user: User) -> None:
    if job.message_type == "photo" and job.media_file_id is not None:
        await bot.send_photo(
            chat_id=user.telegram_id,
            photo=job.media_file_id,
            caption=job.media_caption,
        )
    else:
        await bot.send_message(chat_id=user.telegram_id, text=job.text or "")


async def process_broadcast_queue(session: AsyncSession, bot: Bot) -> None:
    """Process queued broadcasts.

    Resumability: progress is stored on the job's `payload.delivered_user_ids`
    so that if the worker restarts mid-broadcast, the next run resumes from
    where it left off instead of re-spamming everyone.
    """
    result = await session.execute(
        select(BroadcastJob)
        .where(BroadcastJob.status.in_(("queued", "processing")))
        .order_by(BroadcastJob.created_at.asc())
    )
    jobs = list(result.scalars().all())

    for job in jobs:
        job.status = "processing"
        # Commit the "processing" status + (below) the recipient count right
        # away, so progress is DURABLE. The old code only flush()ed and relied
        # on a single commit by the caller after the whole (30+ min) broadcast
        # finished — a worker restart mid-run rolled everything back and
        # re-spammed the entire audience. We now commit incrementally.
        await session.commit()

        payload = dict(job.payload or {})
        delivered: set[str] = set(payload.get("delivered_user_ids") or [])

        user_result = await session.execute(
            select(User).where(User.status == "active", User.is_bot_blocked.is_(False))
        )
        users = list(user_result.scalars().all())
        job.total_recipients = len(users)
        await session.commit()

        # Commit delivery progress every N sends so a crash loses at most N.
        _COMMIT_EVERY = 25
        sent_since_commit = 0

        for user in users:
            if str(user.id) in delivered:
                continue

            await _global_rate_gate()
            try:
                await _send_broadcast_to_user(bot, job, user)
                job.processed_recipients += 1
                delivered.add(str(user.id))
            except TelegramRetryAfter as exc:
                logger.warning("Broadcast hit FloodWait; sleeping %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await _global_rate_gate()
                    await _send_broadcast_to_user(bot, job, user)
                    job.processed_recipients += 1
                    delivered.add(str(user.id))
                except Exception:
                    job.failed_recipients += 1
            except TelegramForbiddenError:
                user.is_bot_blocked = True
                job.failed_recipients += 1
                # Still mark as delivered so we don't retry forever.
                delivered.add(str(user.id))
            except Exception:
                job.failed_recipients += 1

            # Persist progress incrementally so a worker crash doesn't lose it.
            # IMPORTANT: assign a brand-new dict every time. payload is a plain
            # JSONB column (no MutableDict), so after the first flush the
            # committed snapshot holds a reference to the assigned dict object;
            # mutating it in place and re-assigning the SAME object compares
            # equal to itself and the UPDATE silently drops the payload column —
            # delivered_user_ids would then never advance past the first send.
            job.payload = {**payload, "delivered_user_ids": sorted(delivered)}
            sent_since_commit += 1
            if sent_since_commit >= _COMMIT_EVERY:
                await session.commit()
                sent_since_commit = 0
            else:
                await session.flush()

        job.status = "finished"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
