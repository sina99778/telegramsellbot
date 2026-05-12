from __future__ import annotations

import asyncio
import logging
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
                    text="❌ هیچ کانفیگی با این مشخصات یافت نشد."
                )
                return

            await bot.edit_message_text(
                chat_id=admin_telegram_id,
                message_id=progress_message_id,
                text=f"🔄 در حال پردازش {total} کانفیگ...\n\nلطفاً صبور باشید."
            )

            updated_count = 0
            failed_count = 0

            # Process in chunks of 10 to prevent large transactions and expired object issues
            for i in range(0, total, 10):
                chunk_ids = subscription_ids[i:i+10]
                
                chunk_stmt = (
                    select(Subscription)
                    .options(
                        selectinload(Subscription.xui_client)
                        .selectinload(XUIClientRecord.inbound)
                        .selectinload(XUIInboundRecord.server),
                        selectinload(Subscription.plan),
                        selectinload(Subscription.user),
                    )
                    .where(Subscription.id.in_(chunk_ids))
                )
                chunk_result = await session.execute(chunk_stmt)
                chunk_subscriptions = list(chunk_result.scalars().unique().all())

                for subscription in chunk_subscriptions:
                    try:
                        await apply_renewal(
                            session=session,
                            subscription=subscription,
                            renew_type=gift_type,
                            amount=amount,
                        )
                        updated_count += 1
                        
                        # Send notification to user
                        if subscription.user and subscription.user.telegram_id:
                            unit = "روز" if gift_type == "time" else "گیگابایت"
                            conf_name = subscription.xui_client.username if subscription.xui_client else "نامشخص"
                            msg = f"🎁 هدیه جدید از طرف مدیریت!\n\n مقدار {amount:g} {unit} به سرویس شما اضافه شد.\n\nنام کانفیگ: {conf_name}"
                            try:
                                await bot.send_message(subscription.user.telegram_id, msg)
                            except TelegramAPIError:
                                pass
                    except Exception as e:
                        logger.warning("Failed to gift sub %s: %s", subscription.id, e)
                        failed_count += 1
                        # We don't need await session.rollback() because apply_renewal uses begin_nested()

                # Commit chunk
                await session.commit()
                await asyncio.sleep(0.5)  # Let event loop breathe

                try:
                    await bot.edit_message_text(
                        chat_id=admin_telegram_id,
                        message_id=progress_message_id,
                        text=f"🔄 در حال پردازش...\n\n"
                             f"📊 پیشرفت: {min(i + 10, total)} از {total}\n"
                             f"✅ موفق: {updated_count}\n"
                             f"❌ ناموفق: {failed_count}"
                    )
                except TelegramAPIError:
                    pass
            
            # Log audit at the end
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

            await bot.edit_message_text(
                chat_id=admin_telegram_id,
                message_id=progress_message_id,
                text="✅ هدیه گروهی با موفقیت پایان یافت.\n\n"
                     f"کل کانفیگ‌ها: {total}\n"
                     f"موفق: {updated_count}\n"
                     f"ناموفق: {failed_count}"
            )
    except Exception as exc:
        logger.error("Background bulk gift failed: %s", exc, exc_info=True)
        try:
            await bot.edit_message_text(
                chat_id=admin_telegram_id,
                message_id=progress_message_id,
                text="❌ خطای غیرمنتظره در حین پردازش هدایا رخ داد."
            )
        except Exception:
            pass
