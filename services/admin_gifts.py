from __future__ import annotations

import asyncio
import logging
import traceback
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from core.database import AsyncSessionFactory
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord
from repositories.audit import AuditLogRepository
from services.renewal import apply_renewal

logger = logging.getLogger(__name__)

ACTIVE_GIFT_STATUSES = ("active", "pending_activation")
ALL_GIFT_STATUSES = ("active", "pending_activation", "expired")


def get_gift_statuses(status_scope: str) -> tuple[str, ...]:
    if status_scope == "active":
        return ACTIVE_GIFT_STATUSES
    if status_scope == "all":
        return ALL_GIFT_STATUSES
    raise ValueError("Invalid gift status scope.")


async def _gift_single_subscription(
    sub_id: UUID,
    gift_type: str,
    amount: float,
) -> tuple[bool, int | None, str | None]:
    """Process gift for a single subscription in its own isolated session.

    Returns (success, user_telegram_id, config_name).
    """
    async with AsyncSessionFactory() as session:
        try:
            subscription = await session.scalar(
                select(Subscription)
                .options(
                    selectinload(Subscription.xui_client)
                    .selectinload(XUIClientRecord.inbound)
                    .selectinload(XUIInboundRecord.server),
                    selectinload(Subscription.plan),
                    selectinload(Subscription.user),
                )
                .where(Subscription.id == sub_id)
            )
            if subscription is None:
                return False, None, None

            await apply_renewal(
                session=session,
                subscription=subscription,
                renew_type=gift_type,
                amount=amount,
            )
            await session.commit()

            tg_id = subscription.user.telegram_id if subscription.user else None
            conf_name = subscription.xui_client.username if subscription.xui_client else None
            return True, tg_id, conf_name
        except Exception as e:
            logger.warning(
                "Failed to gift sub %s: %s\n%s",
                sub_id, e, traceback.format_exc(),
            )
            await session.rollback()
            return False, None, None


async def grant_bulk_subscription_gift_background(
    bot: Bot,
    admin_telegram_id: int,
    admin_user_id: UUID,
    progress_message_id: int,
    gift_type: str,
    amount: float,
    status_scope: str,
    server_id: UUID | None = None,
) -> None:
    """Run bulk gifting in background and report progress to admin."""
    if gift_type not in {"time", "volume"}:
        return
    if amount <= 0:
        return

    try:
        # Step 1: Fetch all matching subscription IDs in one query
        async with AsyncSessionFactory() as session:
            statuses = get_gift_statuses(status_scope)
            stmt = select(Subscription.id).where(Subscription.status.in_(statuses))
            if server_id is not None:
                stmt = (
                    stmt.join(XUIClientRecord, XUIClientRecord.subscription_id == Subscription.id)
                    .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
                    .where(XUIInboundRecord.server_id == server_id)
                )
            result = await session.execute(stmt)
            subscription_ids = list(result.scalars().unique().all())

        total = len(subscription_ids)

        if total == 0:
            await bot.edit_message_text(
                chat_id=admin_telegram_id,
                message_id=progress_message_id,
                text="❌ هیچ کانفیگی با این مشخصات یافت نشد.",
            )
            return

        await bot.edit_message_text(
            chat_id=admin_telegram_id,
            message_id=progress_message_id,
            text=f"🔄 در حال پردازش {total} کانفیگ...\n\nلطفاً صبور باشید.",
        )

        updated_count = 0
        failed_count = 0

        # Step 2: Process each subscription in its own session
        for i, sub_id in enumerate(subscription_ids):
            success, tg_id, conf_name = await _gift_single_subscription(
                sub_id, gift_type, amount,
            )

            if success:
                updated_count += 1
                # Send notification to user
                if tg_id:
                    unit = "روز" if gift_type == "time" else "گیگابایت"
                    msg = (
                        f"🎁 هدیه جدید از طرف مدیریت!\n\n"
                        f"مقدار {amount:g} {unit} به سرویس شما اضافه شد.\n\n"
                        f"نام کانفیگ: {conf_name or 'نامشخص'}"
                    )
                    try:
                        await bot.send_message(tg_id, msg)
                    except TelegramAPIError:
                        pass
            else:
                failed_count += 1

            # Update progress every 5 subscriptions
            if (i + 1) % 5 == 0 or (i + 1) == total:
                try:
                    await bot.edit_message_text(
                        chat_id=admin_telegram_id,
                        message_id=progress_message_id,
                        text=(
                            f"🔄 در حال پردازش...\n\n"
                            f"📊 پیشرفت: {i + 1} از {total}\n"
                            f"✅ موفق: {updated_count}\n"
                            f"❌ ناموفق: {failed_count}"
                        ),
                    )
                except TelegramAPIError:
                    pass

            # Brief sleep to avoid overwhelming the event loop / DB
            await asyncio.sleep(0.1)

        # Step 3: Log audit
        async with AsyncSessionFactory() as session:
            await AuditLogRepository(session).log_action(
                actor_user_id=admin_user_id,
                action="bulk_subscription_gift",
                entity_type="subscription",
                entity_id=admin_user_id,
                payload={
                    "gift_type": gift_type,
                    "amount": amount,
                    "status_scope": status_scope,
                    "server_id": str(server_id) if server_id else None,
                    "matched": total,
                    "updated": updated_count,
                    "failed": failed_count,
                },
            )
            await session.commit()

        # Step 4: Final message
        await bot.edit_message_text(
            chat_id=admin_telegram_id,
            message_id=progress_message_id,
            text=(
                "✅ هدیه گروهی با موفقیت پایان یافت.\n\n"
                f"کل کانفیگ‌ها: {total}\n"
                f"موفق: {updated_count}\n"
                f"ناموفق: {failed_count}"
            ),
        )
    except Exception as exc:
        logger.error(
            "Background bulk gift failed: %s\n%s",
            exc, traceback.format_exc(),
        )
        try:
            await bot.edit_message_text(
                chat_id=admin_telegram_id,
                message_id=progress_message_id,
                text=f"❌ خطای غیرمنتظره در حین پردازش هدایا رخ داد.\n\n{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
