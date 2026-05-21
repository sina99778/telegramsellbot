"""
Admin notification helper.
Sends purchase/renewal alerts to all admin/owner users.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.user import User

logger = logging.getLogger(__name__)


async def notify_admins(
    session: AsyncSession,
    bot: Bot,
    text: str,
) -> None:
    """Send a text notification to all admin/owner users (parallel)."""
    admin_telegram_ids: set[int] = set()

    if settings.owner_telegram_id:
        admin_telegram_ids.add(settings.owner_telegram_id)

    try:
        result = await session.execute(
            select(User.telegram_id).where(
                User.role.in_(["admin", "owner"]),
            )
        )
        for row in result.scalars().all():
            admin_telegram_ids.add(row)
    except Exception as exc:
        logger.warning("Failed to query admin users from DB: %s", exc)

    if not admin_telegram_ids:
        logger.warning("No admin telegram IDs found — notification not sent")
        return

    logger.info("Notifying %d admin(s): %s", len(admin_telegram_ids), admin_telegram_ids)

    async def _send(tg_id: int) -> None:
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
            logger.info("Admin notification sent to %s", tg_id)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Could not notify admin tg=%s: %s", tg_id, exc)
        except Exception as exc:
            logger.error("Unexpected error notifying admin tg=%s: %s", tg_id, exc)

    await asyncio.gather(*[_send(tg_id) for tg_id in admin_telegram_ids], return_exceptions=True)


async def notify_sales_event(
    session: AsyncSession,
    bot: Bot,
    text: str,
    *,
    fallback_to_admins: bool = True,
) -> None:
    """Send a purchase/renewal/topup-style notification.

    Routing rules:
    1. If the admin has configured a sales-report channel (AppSettings
       key ``notifications.sales_channel``), send the message there ONLY.
       This keeps admin DMs quiet — one shared channel for every sale.
    2. If the channel is not configured OR the send to the channel
       fails (forbidden, bad request, kicked, …), fall back to the
       legacy "DM every admin" behaviour so we never silently drop a
       sale notification.
    """
    from repositories.settings import AppSettingsRepository

    chat_id: int | None = None
    try:
        chat_id = await AppSettingsRepository(session).get_sales_report_chat_id()
    except Exception as exc:
        logger.warning("Failed to load sales-report channel setting: %s", exc)

    if chat_id is not None:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            return
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning(
                "Sales-report channel %s rejected the message (%s); "
                "falling back to admin DMs",
                chat_id, exc,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error sending to sales-report channel %s: %s",
                chat_id, exc,
            )

    if fallback_to_admins:
        await notify_admins(session, bot, text)
