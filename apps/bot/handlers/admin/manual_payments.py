"""
Admin handler for approving/rejecting manual crypto payments.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from models.payment import Payment
from models.user import User
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

router = Router(name="admin-manual-payments")
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data.startswith("admin:manual_pay:approve:"))
async def approve_manual_payment(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    await callback.answer("⏳ در حال تأیید...")

    payment_id_str = callback.data.split(":")[-1]
    try:
        payment_id = UUID(payment_id_str)
    except ValueError:
        await safe_edit_or_send(callback, "❌ شناسه پرداخت نامعتبر.")
        return

    payment = await session.scalar(
        Payment.__table__.select().where(Payment.id == payment_id)
    )
    # Re-fetch with ORM for relationship access
    payment = await session.get(Payment, payment_id)
    if payment is None:
        await safe_edit_or_send(callback, "❌ پرداخت یافت نشد.")
        return

    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_or_send(callback, f"⚠️ این پرداخت قبلاً پردازش شده. وضعیت: {payment.payment_status}")
        return

    # Process the payment (credit wallet)
    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=payment.price_amount,
        )
    except Exception as exc:
        logger.error("Failed to process manual payment %s: %s", payment.id, exc, exc_info=True)
        await safe_edit_or_send(
            callback,
            f"❌ خطا در پردازش پرداخت:\n{str(exc)[:300]}",
        )
        return

    # Update admin message
    tx_hash = (payment.callback_payload or {}).get("tx_hash", "N/A")
    admin_user = await session.get(User, payment.user_id)
    user_info = f"{admin_user.first_name or '-'} ({admin_user.telegram_id})" if admin_user else "?"

    await safe_edit_or_send(
        callback,
        f"✅ **پرداخت دستی تأیید شد**\n\n"
        f"👤 کاربر: {user_info}\n"
        f"💵 مبلغ: {payment.price_amount:.2f} USD\n"
        f"🔗 TX Hash: `{tx_hash}`\n\n"
        f"💰 مبلغ به کیف پول کاربر واریز شد.\n"
        f"✅ تأیید شده توسط ادمین {callback.from_user.first_name}",
    )

    # Notify user
    if admin_user:
        try:
            bot = callback.bot
            await bot.send_message(
                admin_user.telegram_id,
                f"✅ پرداخت شما تأیید شد!\n\n"
                f"💰 مبلغ {payment.price_amount:.2f} USD به کیف پول شما واریز شد.\n"
                f"اکنون می‌توانید از موجودی کیف پول خرید کنید.",
            )
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Could not notify user about approval: %s", exc)


@router.callback_query(F.data.startswith("admin:manual_pay:reject:"))
async def reject_manual_payment(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    await callback.answer()

    payment_id_str = callback.data.split(":")[-1]
    try:
        payment_id = UUID(payment_id_str)
    except ValueError:
        await safe_edit_or_send(callback, "❌ شناسه پرداخت نامعتبر.")
        return

    payment = await session.get(Payment, payment_id)
    if payment is None:
        await safe_edit_or_send(callback, "❌ پرداخت یافت نشد.")
        return

    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_or_send(callback, f"⚠️ این پرداخت قبلاً پردازش شده. وضعیت: {payment.payment_status}")
        return

    payment.payment_status = "rejected"
    await session.flush()

    tx_hash = (payment.callback_payload or {}).get("tx_hash", "N/A")
    rejected_user = await session.get(User, payment.user_id)
    user_info = f"{rejected_user.first_name or '-'} ({rejected_user.telegram_id})" if rejected_user else "?"

    await safe_edit_or_send(
        callback,
        f"❌ **پرداخت دستی رد شد**\n\n"
        f"👤 کاربر: {user_info}\n"
        f"💵 مبلغ: {payment.price_amount:.2f} USD\n"
        f"🔗 TX Hash: `{tx_hash}`\n\n"
        f"❌ رد شده توسط ادمین {callback.from_user.first_name}",
    )

    # Notify user
    if rejected_user:
        try:
            bot = callback.bot
            await bot.send_message(
                rejected_user.telegram_id,
                f"❌ پرداخت شما رد شد.\n\n"
                f"💰 مبلغ: {payment.price_amount:.2f} USD\n"
                f"🔗 TX Hash: `{tx_hash[:20]}...`\n\n"
                "در صورت مشکل با پشتیبانی تماس بگیرید.",
            )
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Could not notify user about rejection: %s", exc)
