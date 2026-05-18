"""
Admin handler for approving/rejecting manual crypto payments.
"""
from __future__ import annotations

import logging
from uuid import UUID

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from models.payment import Payment
from models.user import User
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

router = Router(name="admin-manual-payments")
router.callback_query.middleware(AdminOnlyMiddleware())


async def _build_payment_context(session: AsyncSession, payment: Payment) -> str:
    """Build a context string with red flags for the admin reviewing a manual
    payment: how old the account is, how many recent rejections, the user's
    current wallet balance. Helps spot bot-spam / fraud-pattern accounts."""
    user = await session.get(User, payment.user_id)
    if user is None:
        return ""

    # Account age
    created = user.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created).days if created else None

    # Recent rejections (last 30 days)
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rejected = int(
        await session.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.user_id == user.id,
                Payment.payment_status.in_(("rejected", "failed", "expired")),
                Payment.created_at >= cutoff,
            )
        ) or 0
    )

    # Successful payments lifetime
    paid_total = int(
        await session.scalar(
            select(func.count()).select_from(Payment).where(
                Payment.user_id == user.id,
                Payment.payment_status == "finished",
            )
        ) or 0
    )

    balance_str = "—"
    if user.wallet is not None:
        balance_str = f"{float(user.wallet.balance):.2f}$"

    flags: list[str] = []
    if age_days is not None and age_days < 3:
        flags.append("🆕 اکانت تازه‌ساز")
    if rejected >= 3:
        flags.append(f"🚩 {rejected} رد در ۳۰ روز اخیر")
    if paid_total == 0:
        flags.append("👶 اولین پرداخت موفق")

    flag_line = ("\n⚠️ " + " | ".join(flags)) if flags else ""
    return (
        "\n━━━━━━━━━━\n"
        "<b>سابقه کاربر:</b>\n"
        f"• سن اکانت: <b>{age_days if age_days is not None else '—'}</b> روز\n"
        f"• موجودی کیف پول: <b>{balance_str}</b>\n"
        f"• پرداخت‌های موفق: <b>{paid_total}</b>\n"
        f"• رد در ۳۰ روز اخیر: <b>{rejected}</b>"
        f"{flag_line}"
    )


@router.callback_query(F.data.startswith("mp:ok:") & ~F.data.contains(":final:"))
async def approve_manual_payment_preview(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """First click: show context (red flags, history) and require a second
    confirming click. This catches misclicks and gives the admin the data
    they need to spot bot-spam patterns before crediting the wallet."""
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

    context = await _build_payment_context(session, payment)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید نهایی و واریز", callback_data=f"mp:ok:final:{payment.id}")
    builder.button(text="❌ رد پرداخت", callback_data=f"mp:no:{payment.id}")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        f"🔎 <b>پیش از تأیید نهایی</b>\n"
        f"شناسه پرداخت: <code>{payment.id}</code>\n"
        f"مبلغ: <b>{payment.price_amount:.2f}$</b>"
        f"{context}\n\n"
        "برای تأیید نهایی، دکمه پایین را بزنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("mp:ok:final:"))
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

    # Row-lock the payment so a double-click cannot credit twice. The lock is
    # held until the transaction commits at the end of this handler.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
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
    tx_hash = payload.get("tx_hash") or ("رسید تصویری" if payment.provider == "card_to_card" else "N/A")
    crypto_amt = payload.get("crypto_amount")
    target_user = await session.get(User, payment.user_id)
    user_info = f"<b>{target_user.first_name or '-'}</b> (<code>{target_user.telegram_id}</code>)" if target_user else "?"
    admin_name = callback.from_user.first_name if callback.from_user else "Admin"

    crypto_line = f"🪙 معادل: {crypto_amt} {payment.pay_currency}\n" if crypto_amt else ""
    result_line = (
        "کانفیگ برای کاربر ارسال شد."
        if payment.kind == "direct_purchase"
        else "مبلغ به کیف پول کاربر واریز شد."
    )

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
        f"💰 {result_line}\n"
        f"👤 تأیید: {admin_name}",
    )

    # Notify user
    if target_user:
        try:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            if payment.kind == "direct_purchase":
                await callback.bot.send_message(
                    target_user.telegram_id,
                    "✅ پرداخت شما تایید شد. اگر کانفیگ در پیام جداگانه ارسال نشده باشد، لطفا با پشتیبانی تماس بگیرید.",
                )
                return
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

    # Row-lock the payment so a double-click cannot credit twice. The lock is
    # held until the transaction commits at the end of this handler.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
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
