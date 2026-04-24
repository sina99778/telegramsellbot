"""
Expiry & volume notification job.
Sends notifications to users whose subscription is about to expire or running low on volume.

Deduplication: uses AppSetting to track last notified state per subscription,
preventing repeated alerts for the same threshold.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import utcnow
from core.formatting import format_volume_bytes
from models.app_setting import AppSetting
from models.subscription import Subscription
from models.user import User

logger = logging.getLogger(__name__)

# AppSetting key prefix for tracking which alerts have been sent
_ALERT_KEY_PREFIX = "alert.sub."


async def send_expiry_notifications(session: AsyncSession, bot: Bot) -> None:
    """Notify users about subscriptions expiring within 24 hours."""
    now = utcnow()
    threshold = now + timedelta(hours=24)

    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.status == "active",
            Subscription.ends_at.isnot(None),
            Subscription.ends_at <= threshold,
            Subscription.ends_at > now,
        )
    )
    subscriptions = list(result.scalars().all())

    for sub in subscriptions:
        user = sub.user
        if user is None or user.is_bot_blocked:
            continue

        # Dedup: only send once per subscription for time expiry
        alert_key = f"{_ALERT_KEY_PREFIX}{sub.id}.time_24h"
        if await _already_alerted(session, alert_key):
            continue

        plan_name = sub.plan.name if sub.plan else "نامشخص"
        sub_name = sub.xui_client.username if (sub.xui_client and sub.xui_client.username) else str(sub.id)[:8]
        remaining_hours = max(int((sub.ends_at - now).total_seconds() / 3600), 0)
        volume_remaining = format_volume_bytes(max(sub.volume_bytes - sub.used_bytes, 0))

        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 تمدید سرویس", callback_data="user:my_configs")
        builder.adjust(1)

        text = (
            "⚠️ سرویس شما رو به اتمام است!\n\n"
            f"👤 سرویس: {sub_name}\n"
            f"📦 پلن: {plan_name}\n"
            f"⏰ زمان باقی‌مانده: {remaining_hours} ساعت\n"
            f"💾 حجم باقی‌مانده: {volume_remaining}\n\n"
            "برای تمدید از بخش «سرویس‌های من» اقدام کنید."
        )

        try:
            await bot.send_message(user.telegram_id, text, reply_markup=builder.as_markup())
            await _mark_alerted(session, alert_key)
        except TelegramForbiddenError:
            user.is_bot_blocked = True
        except Exception as exc:
            logger.warning("Failed to send expiry notification to %s: %s", user.telegram_id, exc)

    # ─── Volume-based alerts ─────────────────────────────────────────────
    await _send_volume_warnings(session, bot)


async def _send_volume_warnings(session: AsyncSession, bot: Bot) -> None:
    """Notify users when volume usage exceeds 90% or 95%."""
    volume_result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.status == "active",
            Subscription.volume_bytes > 0,
        )
    )
    volume_subs = list(volume_result.scalars().all())

    for sub in volume_subs:
        if sub.volume_bytes <= 0:
            continue

        usage_ratio = sub.used_bytes / sub.volume_bytes
        user = sub.user
        if user is None or user.is_bot_blocked:
            continue

        # Determine which threshold to alert for
        if usage_ratio >= 0.95:
            threshold_key = "vol_95"
            pct_remaining = max(100 - round(usage_ratio * 100), 0)
            emoji = "🔴"
            urgency = "بحرانی"
        elif usage_ratio >= 0.90:
            threshold_key = "vol_90"
            pct_remaining = max(100 - round(usage_ratio * 100), 0)
            emoji = "🟡"
            urgency = "کم"
        else:
            continue

        # Dedup check
        alert_key = f"{_ALERT_KEY_PREFIX}{sub.id}.{threshold_key}"
        if await _already_alerted(session, alert_key):
            continue

        plan_name = sub.plan.name if sub.plan else "نامشخص"
        sub_name = sub.xui_client.username if (sub.xui_client and sub.xui_client.username) else str(sub.id)[:8]
        volume_remaining = format_volume_bytes(max(sub.volume_bytes - sub.used_bytes, 0))
        volume_total = format_volume_bytes(sub.volume_bytes)
        pct_used = round(usage_ratio * 100)

        builder = InlineKeyboardBuilder()
        builder.button(text="💾 افزایش حجم", callback_data="user:my_configs")
        builder.adjust(1)

        text = (
            f"{emoji} هشدار: حجم سرویس شما {urgency} است!\n\n"
            f"👤 سرویس: {sub_name}\n"
            f"📦 پلن: {plan_name}\n"
            f"📊 مصرف: {pct_used}% ({format_volume_bytes(sub.used_bytes)} از {volume_total})\n"
            f"💾 باقی‌مانده: {volume_remaining} ({pct_remaining}%)\n\n"
        )

        if usage_ratio >= 0.95:
            text += "❗ حجم سرویس شما تقریباً تمام شده!\nبرای جلوگیری از قطع سرویس، هرچه سریع‌تر حجم اضافه کنید."
        else:
            text += "برای افزایش حجم از بخش «سرویس‌های من» → تمدید اقدام کنید."

        try:
            await bot.send_message(user.telegram_id, text, reply_markup=builder.as_markup())
            await _mark_alerted(session, alert_key)
            logger.info(
                "Volume warning sent: user=%s, sub=%s, usage=%d%%",
                user.telegram_id, sub.id, pct_used,
            )
        except TelegramForbiddenError:
            user.is_bot_blocked = True
        except Exception as exc:
            logger.warning("Failed to send volume warning to %s: %s", user.telegram_id, exc)


# ─── Alert deduplication helpers ──────────────────────────────────────────────


async def _already_alerted(session: AsyncSession, key: str) -> bool:
    """Check if an alert has already been sent for this key."""
    record = await session.get(AppSetting, key)
    return record is not None


async def _mark_alerted(session: AsyncSession, key: str) -> None:
    """Mark an alert as sent."""
    record = await session.get(AppSetting, key)
    if record is None:
        record = AppSetting(
            key=key,
            value_json={"sent_at": utcnow().isoformat()},
        )
        session.add(record)
        await session.flush()
