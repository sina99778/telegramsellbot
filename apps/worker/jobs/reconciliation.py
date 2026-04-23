"""
Reconciliation job: finds stuck payments and alerts admins.

Runs periodically to detect:
- Payments that are 'waiting' for more than 24 hours
- Payments that are paid but not provisioned
- Failed payments needing manual review
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.payment import Payment

logger = logging.getLogger(__name__)


async def run_reconciliation(session: AsyncSession, bot: Bot) -> None:
    """Find stuck payments and alert admin."""
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # 1. Paid but not provisioned (direct_purchase)
    stuck_count = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.actually_paid.isnot(None),
            Payment.kind == "direct_purchase",
            or_(
                ~Payment.callback_payload.has_key("provisioned"),
                Payment.callback_payload["provisioned"].as_boolean().is_(False),
            ),
        )
    ) or 0

    # 2. Waiting payments older than 24h
    stale_waiting = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["waiting", "confirming"]),
            Payment.actually_paid.is_(None),
            Payment.created_at < cutoff_24h,
        )
    ) or 0

    # 3. Failed payments in last 24h
    recent_failed = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["failed", "expired"]),
            Payment.created_at >= cutoff_24h,
        )
    ) or 0

    # 4. Manual crypto payments pending admin approval for >24h
    stale_manual = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["pending_approval", "waiting_hash"]),
            Payment.created_at < cutoff_24h,
        )
    ) or 0

    # Only alert if there are issues
    if stuck_count == 0 and stale_waiting == 0 and recent_failed == 0 and stale_manual == 0:
        logger.info("Reconciliation: no issues found")
        return

    alert_text = (
        "🔔 گزارش Reconciliation خودکار\n\n"
        f"⚠️ پرداخت موفق بدون تحویل: {stuck_count}\n"
        f"⏳ پرداخت در انتظار (+24 ساعت): {stale_waiting}\n"
        f"❌ پرداخت ناموفق (24 ساعت اخیر): {recent_failed}\n"
        f"🔐 پرداخت دستی منتظر تأیید (+24 ساعت): {stale_manual}\n\n"
        "از منوی 🔧 Recovery اقدام کنید."
    )

    logger.warning(
        "Reconciliation alert: stuck=%d, stale_waiting=%d, recent_failed=%d",
        stuck_count, stale_waiting, recent_failed,
    )

    # Send to owner
    if settings.owner_telegram_id:
        try:
            await bot.send_message(settings.owner_telegram_id, alert_text)
        except Exception as exc:
            logger.error("Failed to send reconciliation alert: %s", exc)
