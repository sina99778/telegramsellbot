"""
Admin handler for approving/rejecting manual crypto payments.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from models.payment import Payment
from models.user import User
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

router = Router(name="admin-manual-payments")
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data.startswith("mp:ok:"))
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

    payment = await session.get(Payment, payment_id)
    if payment is None:
        await safe_edit_or_send(callback, "❌ پرداخت یافت نشد.")
        return

    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_or_send(callback, f"⚠️ این پرداخت قبلاً پردازش شده.\nوضعیت: {payment.payment_status}")
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
    payload = payment.callback_payload or {}
    tx_hash = payload.get("tx_hash", "N/A")
    crypto_amt = payload.get("crypto_amount")
    target_user = await session.get(User, payment.user_id)
    user_info = f"<b>{target_user.first_name or '-'}</b> (<code>{target_user.telegram_id}</code>)" if target_user else "?"
    admin_name = callback.from_user.first_name if callback.from_user else "Admin"

    crypto_line = f"🪙 معادل: {crypto_amt} {payment.pay_currency}\n" if crypto_amt else ""

    await safe_edit_or_send(
        callback,
        "✅━━━━━━━━━━━━━━━━━━━━━✅\n"
        "  پرداخت تأیید شد\n"
        "✅━━━━━━━━━━━━━━━━━━━━━✅\n\n"
        f"👤 کاربر: {user_info}\n"
        f"💵 مبلغ: <b>{payment.price_amount:.2f} USD</b>\n"
        f"💱 ارز: {payment.pay_currency}\n"
        f"{crypto_line}"
        f"🔗 Hash: <code>{tx_hash}</code>\n\n"
        f"💰 مبلغ به کیف پول کاربر واریز شد.\n"
        f"👤 تأیید: {admin_name}",
    )

    # Notify user
    if target_user:
        try:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            user_builder = InlineKeyboardBuilder()
            user_builder.button(text="🛒 خرید کانفیگ", callback_data="wallet:topup")
            user_builder.adjust(1)
            await callback.bot.send_message(
                target_user.telegram_id,
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "  ✅ پرداخت تأیید شد!\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 مبلغ <b>{payment.price_amount:.2f} USD</b> به کیف پول\n"
                "شما واریز شد.\n\n"
                "🛒 اکنون می‌توانید از بخش «خرید کانفیگ»\n"
                "سرویس مورد نظر را خریداری کنید.\n"
                "روش پرداخت «👛 کیف پول» را انتخاب کنید.",
                reply_markup=user_builder.as_markup(),
            )
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Could not notify user about approval: %s", exc)


@router.callback_query(F.data.startswith("mp:no:"))
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
        await safe_edit_or_send(callback, f"⚠️ این پرداخت قبلاً پردازش شده.\nوضعیت: {payment.payment_status}")
        return

    payment.payment_status = "rejected"
    await session.flush()

    payload = payment.callback_payload or {}
    tx_hash = payload.get("tx_hash", "N/A")
    rejected_user = await session.get(User, payment.user_id)
    user_info = f"<b>{rejected_user.first_name or '-'}</b> (<code>{rejected_user.telegram_id}</code>)" if rejected_user else "?"
    admin_name = callback.from_user.first_name if callback.from_user else "Admin"

    await safe_edit_or_send(
        callback,
        "❌━━━━━━━━━━━━━━━━━━━━━❌\n"
        "  پرداخت رد شد\n"
        "❌━━━━━━━━━━━━━━━━━━━━━❌\n\n"
        f"👤 کاربر: {user_info}\n"
        f"💵 مبلغ: <b>{payment.price_amount:.2f} USD</b>\n"
        f"💱 ارز: {payment.pay_currency}\n"
        f"🔗 Hash: <code>{tx_hash}</code>\n\n"
        f"❌ رد توسط: {admin_name}",
    )

    # Notify user
    if rejected_user:
        try:
            bot = callback.bot
            await bot.send_message(
                rejected_user.telegram_id,
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "  ❌ پرداخت رد شد\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💵 مبلغ: <b>{payment.price_amount:.2f} USD</b>\n"
                f"🔗 Hash: <code>{tx_hash}</code>\n\n"
                "⚠️ اگر واقعاً پرداخت انجام شده،\n"
                "لطفاً با پشتیبانی تماس بگیرید.",
            )
        except TelegramForbiddenError:
            pass
        except Exception as exc:
            logger.warning("Could not notify user about rejection: %s", exc)
