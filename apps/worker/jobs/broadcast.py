from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.broadcast import BroadcastJob
from models.user import User


async def process_broadcast_queue(session: AsyncSession, bot: Bot) -> None:
    result = await session.execute(
        select(BroadcastJob)
        .where(BroadcastJob.status == "queued")
        .order_by(BroadcastJob.created_at.asc())
    )
    jobs = list(result.scalars().all())

    for job in jobs:
        job.status = "processing"
        await session.flush()

        user_result = await session.execute(
            select(User).where(User.status == "active", User.is_bot_blocked.is_(False))
        )
        users = list(user_result.scalars().all())
        job.total_recipients = len(users)
        await session.flush()

        for user in users:
            try:
                if job.message_type == "photo" and job.media_file_id is not None:
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=job.media_file_id,
                        caption=job.media_caption,
                    )
                else:
                    await bot.send_message(chat_id=user.telegram_id, text=job.text or "")
                job.processed_recipients += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramForbiddenError:
                user.is_bot_blocked = True
                job.failed_recipients += 1
            except Exception:
                job.failed_recipients += 1

            await session.flush()
            await asyncio.sleep(0.05)

        job.status = "finished"
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()
