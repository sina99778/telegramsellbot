"""
Admin handler for approving/rejecting manual crypto payments.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_caption_or_text
from models.payment import Payment
from models.user import User
from models.wallet import Wallet
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

router = Router(name="admin-manual-payments")
router.callback_query.middleware(AdminOnlyMiddleware())
router.message.middleware(AdminOnlyMiddleware())


async def _build_payment_context(session: AsyncSession, payment: Payment) -> str:
    """Build a context string with red flags for the admin reviewing a manual
    payment.

    NOTE: in async SQLAlchemy, lazy-loaded relationships raise MissingGreenlet
    when accessed outside a greenlet-spawn context. We therefore (a) eager-load
    the User → Wallet relationship via selectinload, and (b) fall back to a
    direct Wallet query if it still comes back unloaded.
    """
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet))
        .where(User.id == payment.user_id)
    )
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

    # Balance — never touch `user.wallet` directly because the relationship
    # may not be greenlet-loaded on every code path. Always query Wallet
    # explicitly when we need the live number.
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
    balance_str = f"{float(wallet.balance):.2f}$" if wallet is not None else "—"

    flags: list[str] = []
    if age_days is not None and age_days < 3:
        flags.append("🆕 اکانت تازه‌ساز")
    if rejected >= 3:
        flags.append(f"🚩 {rejected} رد در ۳۰ روز اخیر")
    if paid_total == 0:
        flags.append("👶 اولین پرداخت موفق")

    flag_line = ("\n⚠️ " + " | ".join(flags)) if flags else ""

    # ── OCR fraud assist: does the operator's OWN card appear on the receipt? ──
    ocr_line = ""
    if payment.provider == "card_to_card":
        payload = payment.callback_payload or {}
        file_id = payload.get("receipt_file_id") or payment.provider_payment_id
        if file_id:
            try:
                from services.receipt_ocr import assess_card_receipt
                verdict = await assess_card_receipt(
                    str(file_id),
                    card_number=payload.get("card_number"),
                    card_holder=payload.get("card_holder"),
                    expected_toman=int(payment.pay_amount or 0) or None,
                )
                ocr_line = "\n━━━━━━━━━━\n" + verdict["summary"]
            except Exception:
                ocr_line = ""

    return (
        "\n━━━━━━━━━━\n"
        "<b>سابقه کاربر:</b>\n"
        f"• سن اکانت: <b>{age_days if age_days is not None else '—'}</b> روز\n"
        f"• موجودی کیف پول: <b>{balance_str}</b>\n"
        f"• پرداخت‌های موفق: <b>{paid_total}</b>\n"
        f"• رد در ۳۰ روز اخیر: <b>{rejected}</b>"
        f"{flag_line}"
        f"{ocr_line}"
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
        await safe_edit_caption_or_text(callback, "❌ شناسه پرداخت نامعتبر.")
        return

    payment = await session.get(Payment, payment_id)
    if payment is None:
        await safe_edit_caption_or_text(callback, "❌ پرداخت یافت نشد.")
        return
    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_caption_or_text(callback, f"⚠️ این پرداخت قبلاً پردازش شده.\nوضعیت: {payment.payment_status}")
        return

    context = await _build_payment_context(session, payment)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید نهایی و واریز", callback_data=f"mp:ok:final:{payment.id}")
    builder.button(text="❌ رد پرداخت", callback_data=f"mp:no:{payment.id}")
    builder.adjust(1)
    await safe_edit_caption_or_text(
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
        await safe_edit_caption_or_text(callback, "❌ شناسه پرداخت نامعتبر.")
        return

    # Row-lock the payment so a double-click cannot credit twice. The lock is
    # held until the transaction commits at the end of this handler.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        await safe_edit_caption_or_text(callback, "❌ پرداخت یافت نشد.")
        return

    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_caption_or_text(callback, f"⚠️ این پرداخت قبلاً پردازش شده.\nوضعیت: {payment.payment_status}")
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
        await safe_edit_caption_or_text(
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

    await safe_edit_caption_or_text(
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
        await safe_edit_caption_or_text(callback, "❌ شناسه پرداخت نامعتبر.")
        return

    # Row-lock the payment so a double-click cannot credit twice. The lock is
    # held until the transaction commits at the end of this handler.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        await safe_edit_caption_or_text(callback, "❌ پرداخت یافت نشد.")
        return

    if payment.payment_status not in {"pending_approval", "waiting_hash"}:
        await safe_edit_caption_or_text(callback, f"⚠️ این پرداخت قبلاً پردازش شده.\nوضعیت: {payment.payment_status}")
        return

    payment.payment_status = "rejected"
    await session.flush()

    payload = payment.callback_payload or {}
    tx_hash = payload.get("tx_hash", "N/A")
    rejected_user = await session.get(User, payment.user_id)
    user_info = f"<b>{rejected_user.first_name or '-'}</b> (<code>{rejected_user.telegram_id}</code>)" if rejected_user else "?"
    admin_name = callback.from_user.first_name if callback.from_user else "Admin"

    await safe_edit_caption_or_text(
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


# ─────────────────────────────────────────────────────────────────────────────
#  Receipt archive — view any card-to-card receipt again, even after it was
#  approved/rejected. The receipt photo's Telegram file_id is stored on the
#  payment (callback_payload["receipt_file_id"] / provider_payment_id) and is
#  never wiped, so we can always re-send the image on demand.
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_FA = {
    "pending_approval": "⏳ در انتظار تأیید",
    "waiting_receipt": "📤 منتظر رسید",
    "waiting": "⏳ در انتظار",
    "waiting_hash": "⏳ در انتظار",
    "finished": "✅ تأیید‌شده",
    "rejected": "❌ ردشده",
    "expired": "⌛️ منقضی",
    "failed": "⚠️ ناموفق",
}


def _payment_status_fa(status: str | None) -> str:
    return _STATUS_FA.get(status or "", status or "—")


async def _receipt_caption(session: AsyncSession, payment: Payment) -> str:
    payload = payment.callback_payload or {}
    user = await session.get(User, payment.user_id)
    uname = (user.first_name or user.username or str(user.telegram_id)) if user else "?"
    utg = user.telegram_id if user else "?"
    kind_fa = {
        "wallet_topup": "شارژ کیف پول",
        "direct_purchase": "خرید کانفیگ",
        "direct_renewal": "تمدید سرویس",
    }.get(payment.kind or "", payment.kind or "")
    toman = int(payment.pay_amount or 0)
    created = payment.created_at.strftime("%Y-%m-%d %H:%M") if payment.created_at else "—"
    return (
        "🧾 <b>رسید کارت به کارت</b>\n"
        f"وضعیت: <b>{_payment_status_fa(payment.payment_status)}</b>\n"
        f"نوع: {kind_fa}\n"
        f"👤 {_esc(uname)} (<code>{utg}</code>)\n"
        f"💵 <b>{payment.price_amount:.2f}$</b> | {toman:,} تومان\n"
        f"💳 <code>{_esc(payload.get('card_number') or '—')}</code> — {_esc(payload.get('card_holder') or '—')}\n"
        f"🆔 <code>{payment.id}</code>\n"
        f"🕐 {created}"
    )


async def _send_stored_receipt(bot, chat_id: int, session: AsyncSession, payment: Payment) -> bool:
    """Re-send a stored receipt photo (by its Telegram file_id) to chat_id.
    Falls back to a text summary if no photo / the file_id can't be resent."""
    payload = payment.callback_payload or {}
    file_id = payload.get("receipt_file_id") or payment.provider_payment_id
    caption = await _receipt_caption(session, payment)
    if not file_id:
        await bot.send_message(chat_id, caption + "\n\n⚠️ عکس رسیدی برای این پرداخت ثبت نشده.", parse_mode="HTML")
        return False
    try:
        await bot.send_photo(chat_id, photo=file_id, caption=caption, parse_mode="HTML")
        return True
    except Exception as exc:
        logger.warning("resend receipt photo failed for %s: %s", payment.id, exc)
        await bot.send_message(chat_id, caption + "\n\n⚠️ عکس رسید قابل بازیابی نبود.", parse_mode="HTML")
        return False


@router.callback_query(F.data == "admin:receipts")
async def show_receipts_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Button-driven: list the most recent card-to-card receipts (any status)
    with a button to re-view each one's photo — so an accidentally-approved
    receipt is never lost."""
    await callback.answer()
    rows = (await session.execute(
        select(Payment)
        .options(selectinload(Payment.user))
        .where(Payment.provider == "card_to_card")
        .order_by(Payment.created_at.desc())
        .limit(15)
    )).scalars().all()

    builder = InlineKeyboardBuilder()
    if not rows:
        builder.button(text="🔙 بازگشت", callback_data="admin:finance")
        await safe_edit_caption_or_text(callback, "هیچ رسید کارت‌به‌کارتی ثبت نشده.", reply_markup=builder.as_markup())
        return

    lines = ["🧾 <b>رسیدهای اخیر (کارت به کارت)</b>\n"]
    for p in rows:
        u = p.user
        uname = (u.first_name or u.username or str(u.telegram_id)) if u else "?"
        toman = int(p.pay_amount or 0)
        lines.append(f"{_payment_status_fa(p.payment_status)} • {toman:,}ت • {_esc(uname)}")
        builder.button(text=f"🧾 {toman:,}ت — {uname}"[:60], callback_data=f"rcpt:show:{p.id}")
    builder.button(text="🔙 بازگشت", callback_data="admin:finance")
    builder.adjust(1)
    lines.append("\nبرای دیدن عکسِ هر رسید، دکمه‌اش را بزن.")
    await safe_edit_caption_or_text(callback, "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("rcpt:show:"))
async def show_receipt_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    if callback.message is None:
        return
    try:
        pid = UUID(callback.data.split(":")[-1])
    except ValueError:
        return
    payment = await session.get(Payment, pid)
    if payment is None:
        await callback.message.answer("❌ پرداخت یافت نشد.")
        return
    await _send_stored_receipt(callback.bot, callback.message.chat.id, session, payment)


# ─────────────────────────────────────────────────────────────────────────────
#  /diag_autoconfirm <order_id_or_payment_uuid>
#
#  When a manual-crypto payment doesn't auto-confirm, the admin has no way
#  to figure out WHY without sshing the server. This command answers the
#  question in one bot message: was the row gated out? did the explorer
#  return any TXs? did any TX match? if not — by how much was the amount
#  off?
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: object) -> str:
    """HTML-escape; keep tx hashes / addresses safe inside <code> tags."""
    from html import escape
    return escape(str(text))


async def _load_payment_for_diag(session: AsyncSession, ident: str) -> Payment | None:
    """Try interpreting ``ident`` as a UUID first, then fall back to order_id."""
    # UUID path
    try:
        pid = UUID(ident)
        p = await session.scalar(select(Payment).where(Payment.id == pid))
        if p is not None:
            return p
    except (ValueError, AttributeError):
        pass
    # order_id path
    return await session.scalar(select(Payment).where(Payment.order_id == ident))


@router.message(Command("diag_autoconfirm"))
async def diag_autoconfirm(message: Message, command: CommandObject, session: AsyncSession) -> None:
    """Diagnose why a specific manual-crypto invoice isn't auto-confirming.

    Usage:
        /diag_autoconfirm <order_id_or_payment_uuid>

    The reply contains:
      1. Payment summary (status, amount, address, autoconfirm flag).
      2. Autoconfirm gating verdict.
      3. Raw blockchain probe — every incoming TX in the 24h window, with
         per-TX `amount_matches` verdict against the invoice amount.
      4. Verdict line: "would auto-confirm" / "no matching TX found" /
         "explorer error".
    """
    ident = (command.args or "").strip()
    if not ident:
        await message.answer(
            "<b>/diag_autoconfirm</b>\n"
            "Usage: <code>/diag_autoconfirm &lt;order_id_or_payment_uuid&gt;</code>",
            parse_mode="HTML",
        )
        return

    payment = await _load_payment_for_diag(session, ident)
    if payment is None:
        await message.answer(
            f"❌ Payment <code>{_esc(ident)}</code> not found "
            "(tried both UUID and order_id).",
            parse_mode="HTML",
        )
        return

    # Late imports keep this file importable in environments where
    # services.crypto_autoconfirm transitively imports things we don't need.
    from services.crypto_autoconfirm import (
        AUTOCONFIRM_CURRENCIES,
        amount_matches,
        fetch_incoming,
        is_autoconfirmable,
    )

    payload = payment.callback_payload or {}
    address = payload.get("address")
    cur = (payment.pay_currency or "").strip()
    is_ac = is_autoconfirmable(cur)

    created_at = payment.created_at
    if created_at and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    # ─── 1. Payment summary
    lines: list[str] = []
    lines.append("🔍 <b>diag_autoconfirm</b>")
    lines.append("")
    lines.append("<b>Payment</b>")
    lines.append(f"  id:        <code>{_esc(payment.id)}</code>")
    lines.append(f"  order_id:  <code>{_esc(payment.order_id)}</code>")
    lines.append(f"  provider:  <code>{_esc(payment.provider)}</code>")
    lines.append(f"  status:    <code>{_esc(payment.payment_status)}</code>")
    lines.append(f"  kind:      <code>{_esc(payment.kind)}</code>")
    lines.append(f"  currency:  <code>{_esc(payment.pay_currency)}</code>")
    lines.append(f"  pay_amt:   <code>{_esc(payment.pay_amount)}</code>")
    lines.append(f"  price_amt: <code>{_esc(payment.price_amount)} USD</code>")
    lines.append(f"  created:   <code>{_esc(created_at.isoformat() if created_at else '-')}</code>")
    lines.append(f"  address:   <code>{_esc(address)}</code>")
    lines.append(f"  ac_flag:   <code>{_esc(payload.get('autoconfirm_enabled'))}</code>")

    # ─── 2. Gating verdict
    lines.append("")
    lines.append("<b>Gating</b>")
    if payment.provider != "manual_crypto":
        lines.append(f"  ❌ provider is <code>{_esc(payment.provider)}</code>, not manual_crypto — autoconfirm never looks at this row.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    if payment.payment_status not in ("waiting_hash", "waiting_receipt"):
        lines.append(f"  ❌ status is <code>{_esc(payment.payment_status)}</code> — autoconfirm only polls waiting_hash / waiting_receipt.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    if not is_ac:
        lines.append(f"  ❌ currency <code>{_esc(cur)}</code> is NOT in AUTOCONFIRM_CURRENCIES.")
        lines.append(f"     Allowed: <code>{_esc(sorted(AUTOCONFIRM_CURRENCIES))}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    if not address:
        lines.append("  ❌ callback_payload.address is empty — autoconfirm skips this row.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    if payment.pay_amount is None:
        lines.append("  ❌ pay_amount is NULL — autoconfirm filter excludes this row.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return
    lines.append("  ✅ row passes all autoconfirm filters.")

    # ─── 3. Blockchain probe
    since = (created_at or datetime.now(timezone.utc) - timedelta(hours=24)) - timedelta(seconds=60)
    lines.append("")
    lines.append("<b>Explorer probe</b>")
    lines.append(f"  fetching since: <code>{_esc(since.isoformat(timespec='seconds'))}</code>")

    try:
        txs = await fetch_incoming(currency=cur, address=str(address), since=since)
    except Exception as exc:
        lines.append(f"  ❌ explorer call raised: <code>{_esc(type(exc).__name__)}: {_esc(exc)}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    lines.append(f"  got {len(txs)} tx(s) back")

    any_match = False
    if not txs:
        lines.append("  (nothing yet — either the user hasn't paid, the explorer is rate-limited, or our stored address differs from the on-chain destination)")
    else:
        lines.append("")
        lines.append("<b>Per-TX match</b>")
        # Cap at first 20 to stay under Telegram's 4096-char limit.
        for tx in txs[:20]:
            tx_hash = str(tx.get("hash") or "?")
            tx_amt = tx.get("amount")
            tx_ts = tx.get("timestamp")
            try:
                matches = amount_matches(cur, Decimal(payment.pay_amount), Decimal(tx_amt))
            except Exception:
                matches = False
            any_match = any_match or matches
            mark = "✅" if matches else "  "
            short_hash = tx_hash[:12] + "…" if len(tx_hash) > 14 else tx_hash
            lines.append(
                f"  {mark} <code>{_esc(short_hash)}</code>  "
                f"amt=<code>{_esc(tx_amt)}</code>  "
                f"at <code>{_esc(tx_ts.isoformat(timespec='seconds') if tx_ts else '?')}</code>"
            )
        if len(txs) > 20:
            lines.append(f"  … and {len(txs) - 20} more not shown.")

    # ─── 4. Verdict
    lines.append("")
    lines.append("<b>Verdict</b>")
    if any_match:
        lines.append("  ✅ At least one TX matches the invoice amount.")
        lines.append("     The next autoconfirm poll (≤30 s) should pick it up.")
    elif not txs:
        lines.append("  ⏳ No transactions on the address in the time window.")
        lines.append("     Either the user hasn't paid yet OR the explorer is rate-limited.")
        lines.append("     Set TONCENTER_API_KEY / TRONGRID_API_KEY in .env if this is chronic.")
    else:
        lines.append(f"  ⚠️ {len(txs)} TX(s) arrived but none matched the invoice's amount.")
        lines.append("     Most likely the user rounded the amount — refund/retry by hand.")

    await message.answer("\n".join(lines), parse_mode="HTML")
