"""
Admin Payment Recovery & Reconciliation Panel.

Provides:
- Payment list filtered by problematic statuses
- Retry provisioning for paid-but-not-provisioned payments
- Resend config to user
- Manual status changes with audit trail
- User timeline view
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.utils.messaging import safe_edit_or_send
from core.texts import AdminButtons
from models.order import Order
from models.payment import Payment
from models.subscription import Subscription
from models.user import User
from models.wallet import WalletTransaction
from repositories.audit import AuditLogRepository
from apps.bot.states.admin import GlobalSearchStates

logger = logging.getLogger(__name__)

router = Router(name="admin-recovery")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

PAYMENT_PAGE_SIZE = 8


class RecoveryPaymentCallback(CallbackData, prefix="rec_pay"):
    action: str  # view, retry, resend, refund, mark_failed
    payment_id: UUID


class RecoveryFilterCallback(CallbackData, prefix="rec_filter"):
    filter: str  # stuck, waiting_old, failed, all
    page: int = 1


class TimelineCallback(CallbackData, prefix="timeline"):
    user_id: UUID


# ─── Recovery Main Menu ───────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:recovery")
async def recovery_main_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()

    # Quick counts
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

    waiting_count = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["waiting", "confirming"]),
            Payment.actually_paid.is_(None),
        )
    ) or 0

    failed_count = await session.scalar(
        select(func.count()).select_from(Payment).where(
            Payment.payment_status.in_(["failed", "expired"]),
        )
    ) or 0

    text = (
        "🔧 پنل Recovery و Reconciliation\n\n"
        f"⚠️ پرداخت موفق بدون تحویل: {stuck_count}\n"
        f"⏳ پرداخت‌های در انتظار: {waiting_count}\n"
        f"❌ پرداخت‌های ناموفق: {failed_count}\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"⚠️ بدون تحویل ({stuck_count})",
        callback_data=RecoveryFilterCallback(filter="stuck", page=1).pack(),
    )
    builder.button(
        text=f"⏳ در انتظار ({waiting_count})",
        callback_data=RecoveryFilterCallback(filter="waiting_old", page=1).pack(),
    )
    builder.button(
        text=f"❌ ناموفق ({failed_count})",
        callback_data=RecoveryFilterCallback(filter="failed", page=1).pack(),
    )
    builder.button(
        text="📋 همه پرداخت‌ها",
        callback_data=RecoveryFilterCallback(filter="all", page=1).pack(),
    )
    builder.button(
        text="🔍 جستجوی سراسری",
        callback_data="admin:search",
    )
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Payment List (Filtered) ─────────────────────────────────────────────────


@router.callback_query(RecoveryFilterCallback.filter())
async def recovery_payment_list(
    callback: CallbackQuery,
    callback_data: RecoveryFilterCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()

    stmt = select(Payment).options(selectinload(Payment.user))
    filter_label = ""

    if callback_data.filter == "stuck":
        stmt = stmt.where(
            Payment.actually_paid.isnot(None),
            Payment.kind == "direct_purchase",
            or_(
                ~Payment.callback_payload.has_key("provisioned"),
                Payment.callback_payload["provisioned"].as_boolean().is_(False),
            ),
        )
        filter_label = "⚠️ پرداخت موفق بدون تحویل"
    elif callback_data.filter == "waiting_old":
        stmt = stmt.where(
            Payment.payment_status.in_(["waiting", "confirming"]),
            Payment.actually_paid.is_(None),
        )
        filter_label = "⏳ در انتظار"
    elif callback_data.filter == "failed":
        stmt = stmt.where(Payment.payment_status.in_(["failed", "expired"]))
        filter_label = "❌ ناموفق"
    else:
        filter_label = "📋 همه"

    page = max(callback_data.page, 1)
    offset = (page - 1) * PAYMENT_PAGE_SIZE

    total = await session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0

    stmt = stmt.order_by(Payment.created_at.desc()).offset(offset).limit(PAYMENT_PAGE_SIZE)
    result = await session.execute(stmt)
    payments = list(result.scalars().all())

    if not payments:
        builder = InlineKeyboardBuilder()
        builder.button(text=AdminButtons.BACK, callback_data="admin:recovery")
        builder.adjust(1)
        await safe_edit_or_send(callback, f"{filter_label}\n\nهیچ پرداختی یافت نشد.", reply_markup=builder.as_markup())
        return

    from math import ceil
    total_pages = max(ceil(total / PAYMENT_PAGE_SIZE), 1)

    lines = [f"{filter_label} (صفحه {page}/{total_pages} — {total} مورد)\n"]
    builder = InlineKeyboardBuilder()

    for pay in payments:
        user_name = pay.user.first_name if pay.user else "-"
        status_icon = {"finished": "✅", "confirmed": "✅", "failed": "❌", "expired": "⏰", "waiting": "⏳"}.get(pay.payment_status, "❓")
        provisioned = "✅" if (pay.callback_payload or {}).get("provisioned") else "❌"
        amount = f"{pay.price_amount:.2f}" if pay.price_amount else "-"

        lines.append(
            f"{status_icon} {amount}$ | {pay.kind[:8]} | {user_name}\n"
            f"   ID: {str(pay.id)[:8]} | تحویل: {provisioned}"
        )
        builder.button(
            text=f"🔍 {str(pay.id)[:8]} | {user_name}",
            callback_data=RecoveryPaymentCallback(action="view", payment_id=pay.id).pack(),
        )

    # Pagination
    nav = []
    if page > 1:
        nav.append(("⬅️", RecoveryFilterCallback(filter=callback_data.filter, page=page - 1).pack()))
    nav.append((f"{page}/{total_pages}", "pagination:noop"))
    if page < total_pages:
        nav.append(("➡️", RecoveryFilterCallback(filter=callback_data.filter, page=page + 1).pack()))
    for text_btn, cb in nav:
        builder.button(text=text_btn, callback_data=cb)

    builder.button(text=AdminButtons.BACK, callback_data="admin:recovery")

    rows = [1] * len(payments)
    rows.append(len(nav))
    rows.append(1)
    builder.adjust(*rows)

    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


# ─── Payment Detail ───────────────────────────────────────────────────────────


@router.callback_query(RecoveryPaymentCallback.filter(F.action == "view"))
async def recovery_payment_detail(
    callback: CallbackQuery,
    callback_data: RecoveryPaymentCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()

    payment = await session.scalar(
        select(Payment).options(selectinload(Payment.user)).where(Payment.id == callback_data.payment_id)
    )
    if payment is None:
        await safe_edit_or_send(callback, "پرداخت یافت نشد.")
        return

    user = payment.user
    provisioned = (payment.callback_payload or {}).get("provisioned", False)
    user_link = f"@{user.username}" if user and user.username else (str(user.telegram_id) if user else "-")

    text = (
        f"🔍 جزئیات پرداخت\n\n"
        f"🆔 ID: <code>{payment.id}</code>\n"
        f"👤 کاربر: {user_link}\n"
        f"💰 مبلغ: {payment.price_amount} {payment.price_currency}\n"
        f"📦 نوع: {payment.kind}\n"
        f"🏦 درگاه: {payment.provider}\n"
        f"📊 وضعیت: {payment.payment_status}\n"
        f"💵 پرداخت شده: {payment.actually_paid or '-'}\n"
        f"✅ تحویل شده: {'بله' if provisioned else 'خیر'}\n"
        f"🔗 Provider ID: {payment.provider_payment_id or '-'}\n"
        f"📄 Order ID: {payment.order_id or '-'}\n"
        f"📅 تاریخ: {payment.created_at.strftime('%Y-%m-%d %H:%M') if payment.created_at else '-'}\n"
    )

    builder = InlineKeyboardBuilder()

    # Show relevant actions based on state
    if payment.kind == "direct_purchase" and payment.actually_paid is not None and not provisioned:
        builder.button(
            text="🔄 Retry Provisioning",
            callback_data=RecoveryPaymentCallback(action="retry", payment_id=payment.id).pack(),
        )

    if payment.actually_paid is not None and not provisioned:
        builder.button(
            text="💸 Refund به کیف پول",
            callback_data=RecoveryPaymentCallback(action="refund", payment_id=payment.id).pack(),
        )

    if payment.payment_status not in {"failed", "expired"}:
        builder.button(
            text="❌ Mark as Failed",
            callback_data=RecoveryPaymentCallback(action="mark_failed", payment_id=payment.id).pack(),
        )

    # Resend config if provisioned
    if provisioned:
        builder.button(
            text="📨 ارسال مجدد کانفیگ",
            callback_data=RecoveryPaymentCallback(action="resend", payment_id=payment.id).pack(),
        )

    if user:
        builder.button(
            text="👤 Timeline کاربر",
            callback_data=TimelineCallback(user_id=user.id).pack(),
        )

    builder.button(text=AdminButtons.BACK, callback_data="admin:recovery")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Retry Provisioning ──────────────────────────────────────────────────────


@router.callback_query(RecoveryPaymentCallback.filter(F.action == "retry"))
async def recovery_retry_provisioning(
    callback: CallbackQuery,
    callback_data: RecoveryPaymentCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer("⏳ در حال تلاش مجدد...")

    payment = await session.scalar(
        select(Payment).where(Payment.id == callback_data.payment_id)
    )
    if payment is None:
        await safe_edit_or_send(callback, "پرداخت یافت نشد.")
        return

    if payment.kind != "direct_purchase":
        await safe_edit_or_send(callback, "این پرداخت از نوع خرید مستقیم نیست.")
        return

    if (payment.callback_payload or {}).get("provisioned"):
        await safe_edit_or_send(callback, "✅ این پرداخت قبلاً تحویل داده شده.")
        return

    from services.payment import process_successful_payment

    try:
        await process_successful_payment(
            session=session,
            payment=payment,
            amount_to_credit=payment.price_amount,
        )
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="retry_provisioning",
            entity_type="payment",
            entity_id=payment.id,
            payload={"result": "success"},
        )
        await safe_edit_or_send(callback, f"✅ Provisioning مجدد موفق بود.\n\nPayment: {str(payment.id)[:8]}")
    except Exception as exc:
        logger.error("Admin retry provisioning failed: %s", exc, exc_info=True)
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="retry_provisioning",
            entity_type="payment",
            entity_id=payment.id,
            payload={"result": "failed", "error": str(exc)[:200]},
        )
        await safe_edit_or_send(callback, f"❌ Retry ناموفق:\n{str(exc)[:300]}")


# ─── Resend Config ────────────────────────────────────────────────────────────


@router.callback_query(RecoveryPaymentCallback.filter(F.action == "resend"))
async def recovery_resend_config(
    callback: CallbackQuery,
    callback_data: RecoveryPaymentCallback,
    session: AsyncSession,
    admin_user: User,
    bot: Bot,
) -> None:
    await callback.answer("⏳ در حال ارسال مجدد...")

    payment = await session.scalar(
        select(Payment).options(selectinload(Payment.user)).where(Payment.id == callback_data.payment_id)
    )
    if payment is None or payment.user is None:
        await safe_edit_or_send(callback, "پرداخت یا کاربر یافت نشد.")
        return

    # Find the subscription linked to this payment via its order
    plan_id_str = (payment.callback_payload or {}).get("plan_id")
    if not plan_id_str:
        await safe_edit_or_send(callback, "اطلاعات پلن در payload یافت نشد.")
        return

    # Find most recent subscription for this user+plan after payment
    sub = await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == payment.user_id,
            Subscription.plan_id == UUID(plan_id_str),
            Subscription.status.in_(["active", "pending_activation"]),
        )
        .order_by(Subscription.created_at.desc())
    )

    if sub is None or not sub.sub_link:
        await safe_edit_or_send(callback, "اشتراک فعالی با لینک برای این پرداخت یافت نشد.")
        return

    try:
        text = (
            "📨 ارسال مجدد کانفیگ از سوی پشتیبانی:\n\n"
            f"🔗 ساب لینک:\n{sub.sub_link}"
        )
        await bot.send_message(payment.user.telegram_id, text)

        # QR
        from core.qr import make_qr_bytes
        from aiogram.types import BufferedInputFile
        qr_bytes = make_qr_bytes(sub.sub_link)
        if qr_bytes:
            await bot.send_photo(
                chat_id=payment.user.telegram_id,
                photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
                caption="📷 QR کد کانفیگ",
            )

        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="resend_config",
            entity_type="subscription",
            entity_id=sub.id,
            payload={"payment_id": str(payment.id), "user_telegram_id": payment.user.telegram_id},
        )
        await safe_edit_or_send(callback, f"✅ کانفیگ مجدداً به کاربر ارسال شد.\n\nSub: {str(sub.id)[:8]}")
    except TelegramForbiddenError:
        await safe_edit_or_send(callback, "❌ کاربر ربات را بلاک کرده است.")
    except Exception as exc:
        await safe_edit_or_send(callback, f"❌ خطا در ارسال:\n{str(exc)[:300]}")


# ─── Manual Refund ────────────────────────────────────────────────────────────


@router.callback_query(RecoveryPaymentCallback.filter(F.action == "refund"))
async def recovery_manual_refund(
    callback: CallbackQuery,
    callback_data: RecoveryPaymentCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()

    payment = await session.scalar(
        select(Payment).options(selectinload(Payment.user)).where(Payment.id == callback_data.payment_id)
    )
    if payment is None:
        await safe_edit_or_send(callback, "پرداخت یافت نشد.")
        return

    if payment.actually_paid is None or payment.actually_paid <= 0:
        await safe_edit_or_send(callback, "این پرداخت مبلغی نداشته.")
        return

    from services.wallet.manager import WalletManager

    try:
        wallet_manager = WalletManager(session)
        await wallet_manager.process_transaction(
            user_id=payment.user_id,
            amount=payment.price_amount,
            transaction_type="admin_refund",
            direction="credit",
            currency=payment.price_currency,
            reference_type="payment",
            reference_id=payment.id,
            description="Admin manual refund",
            metadata={"admin_id": str(admin_user.id), "reason": "admin_recovery"},
        )
        payment.payment_status = "refunded"

        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="manual_refund",
            entity_type="payment",
            entity_id=payment.id,
            payload={
                "amount": str(payment.price_amount),
                "user_id": str(payment.user_id),
            },
        )
        await safe_edit_or_send(
            callback,
            f"✅ مبلغ {payment.price_amount} {payment.price_currency} به کیف پول کاربر بازگردانده شد.\n"
            f"Payment: {str(payment.id)[:8]}"
        )
    except Exception as exc:
        logger.error("Admin manual refund failed: %s", exc, exc_info=True)
        await safe_edit_or_send(callback, f"❌ خطا در بازپرداخت:\n{str(exc)[:300]}")


# ─── Mark as Failed ───────────────────────────────────────────────────────────


@router.callback_query(RecoveryPaymentCallback.filter(F.action == "mark_failed"))
async def recovery_mark_failed(
    callback: CallbackQuery,
    callback_data: RecoveryPaymentCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()

    payment = await session.scalar(
        select(Payment).where(Payment.id == callback_data.payment_id)
    )
    if payment is None:
        await safe_edit_or_send(callback, "پرداخت یافت نشد.")
        return

    old_status = payment.payment_status
    payment.payment_status = "failed"

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="mark_payment_failed",
        entity_type="payment",
        entity_id=payment.id,
        payload={"old_status": old_status, "new_status": "failed"},
    )
    await safe_edit_or_send(
        callback,
        f"✅ وضعیت پرداخت {str(payment.id)[:8]} از '{old_status}' به 'failed' تغییر یافت."
    )


# ─── User Timeline ────────────────────────────────────────────────────────────


@router.callback_query(TimelineCallback.filter())
async def user_timeline_view(
    callback: CallbackQuery,
    callback_data: TimelineCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()

    user = await session.scalar(
        select(User).options(selectinload(User.wallet)).where(User.id == callback_data.user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, "کاربر یافت نشد.")
        return

    # Payments
    payments = (await session.execute(
        select(Payment)
        .where(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc())
        .limit(10)
    )).scalars().all()

    # Orders
    orders = (await session.execute(
        select(Order)
        .options(selectinload(Order.plan))
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .limit(10)
    )).scalars().all()

    # Subscriptions
    subs = (await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .limit(10)
    )).scalars().all()

    # Wallet transactions
    txns = (await session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(10)
    )).scalars().all()

    user_name = user.first_name or user.username or str(user.telegram_id)
    balance = f"{user.wallet.balance:.2f}" if user.wallet else "0.00"

    lines = [
        f"📋 Timeline: {user_name} (TG: {user.telegram_id})\n"
        f"💰 موجودی: ${balance}\n"
    ]

    if payments:
        lines.append("\n💳 پرداخت‌ها:")
        for p in payments[:5]:
            prov = "✅" if (p.callback_payload or {}).get("provisioned") else "❌"
            status_icon = {"finished": "✅", "confirmed": "✅", "failed": "❌", "waiting": "⏳"}.get(p.payment_status, "❓")
            date = p.created_at.strftime("%m/%d %H:%M") if p.created_at else "-"
            lines.append(f"  {status_icon} {p.price_amount}$ {p.kind[:8]} | تحویل:{prov} | {date}")

    if orders:
        lines.append("\n📦 سفارش‌ها:")
        for o in orders[:5]:
            plan_name = o.plan.name if o.plan else "-"
            date = o.created_at.strftime("%m/%d %H:%M") if o.created_at else "-"
            lines.append(f"  {o.status} | {plan_name} | {o.amount:.2f}$ | {date}")

    if subs:
        lines.append("\n🔗 اشتراک‌ها:")
        for s in subs[:5]:
            plan_name = s.plan.name if s.plan else "-"
            date = s.created_at.strftime("%m/%d") if s.created_at else "-"
            lines.append(f"  {s.status} | {plan_name} | {date}")

    if txns:
        lines.append("\n💵 تراکنش‌ها:")
        for t in txns[:5]:
            icon = "🟢" if t.direction == "credit" else "🔴"
            date = t.created_at.strftime("%m/%d %H:%M") if t.created_at else "-"
            lines.append(f"  {icon} {t.direction} {t.amount:.2f}$ | {t.type} | {date}")

    builder = InlineKeyboardBuilder()
    # Add payment detail buttons for stuck payments
    for p in payments[:3]:
        if p.actually_paid is not None and not (p.callback_payload or {}).get("provisioned"):
            builder.button(
                text=f"⚠️ Fix {str(p.id)[:8]}",
                callback_data=RecoveryPaymentCallback(action="view", payment_id=p.id).pack(),
            )
    builder.button(text=AdminButtons.BACK, callback_data="admin:recovery")
    builder.adjust(1)

    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())


# ─── Global Search ────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:search")
async def global_search_prompt(callback: CallbackQuery, state) -> None:
    from apps.bot.states.admin import GlobalSearchStates
    from aiogram.fsm.context import FSMContext
    await callback.answer()
    if isinstance(state, FSMContext):
        await state.set_state(GlobalSearchStates.waiting_for_query)
    await safe_edit_or_send(
        callback,
        "🔍 جستجوی سراسری\n\n"
        "هر یک از موارد زیر را ارسال کنید:\n"
        "• Telegram ID (عددی)\n"
        "• یوزرنیم (@username)\n"
        "• UUID پرداخت / سفارش / اشتراک\n"
        "• نام کانفیگ\n\n"
        "برای لغو /cancel بزنید.",
    )


@router.message(GlobalSearchStates.waiting_for_query, F.text)
async def global_search_execute(
    message: Message, session: AsyncSession, state: FSMContext,
) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        await state.clear()
        return

    await state.clear()

    results: list[str] = []
    builder = InlineKeyboardBuilder()

    q_clean = query.lstrip("@")

    # 1. Try as telegram_id
    try:
        tg_id = int(q_clean)
        user = await session.scalar(
            select(User).options(selectinload(User.wallet)).where(User.telegram_id == tg_id)
        )
        if user:
            balance = f"{user.wallet.balance:.2f}" if user.wallet else "0.00"
            results.append(
                f"👤 کاربر: {user.first_name or '-'} | @{user.username or '-'}\n"
                f"   TG: {user.telegram_id} | موجودی: ${balance}"
            )
            builder.button(
                text=f"📋 Timeline {user.first_name or tg_id}",
                callback_data=TimelineCallback(user_id=user.id).pack(),
            )
    except ValueError:
        pass

    # 2. Try as username
    if not results:
        user = await session.scalar(
            select(User).options(selectinload(User.wallet))
            .where(func.lower(User.username) == q_clean.lower())
        )
        if user:
            balance = f"{user.wallet.balance:.2f}" if user.wallet else "0.00"
            results.append(
                f"👤 کاربر: {user.first_name or '-'} | @{user.username or '-'}\n"
                f"   TG: {user.telegram_id} | موجودی: ${balance}"
            )
            builder.button(
                text=f"📋 Timeline {user.first_name or user.username}",
                callback_data=TimelineCallback(user_id=user.id).pack(),
            )

    # 3. Try as UUID (payment, order, subscription)
    try:
        search_uuid = UUID(query)

        payment = await session.scalar(
            select(Payment).options(selectinload(Payment.user)).where(Payment.id == search_uuid)
        )
        if payment:
            u = payment.user
            results.append(
                f"💳 پرداخت: {str(payment.id)[:8]}\n"
                f"   {payment.price_amount}$ | {payment.payment_status} | {payment.kind}\n"
                f"   کاربر: {u.first_name if u else '-'}"
            )
            builder.button(
                text=f"🔍 پرداخت {str(payment.id)[:8]}",
                callback_data=RecoveryPaymentCallback(action="view", payment_id=payment.id).pack(),
            )

        order = await session.scalar(
            select(Order).options(selectinload(Order.user), selectinload(Order.plan)).where(Order.id == search_uuid)
        )
        if order:
            results.append(
                f"📦 سفارش: {str(order.id)[:8]}\n"
                f"   {order.amount:.2f}$ | {order.status} | پلن: {order.plan.name if order.plan else '-'}"
            )

        sub = await session.scalar(
            select(Subscription).options(selectinload(Subscription.user), selectinload(Subscription.plan))
            .where(Subscription.id == search_uuid)
        )
        if sub:
            results.append(
                f"🔗 اشتراک: {str(sub.id)[:8]}\n"
                f"   {sub.status} | پلن: {sub.plan.name if sub.plan else '-'}\n"
                f"   لینک: {sub.sub_link[:40] + '...' if sub.sub_link and len(sub.sub_link) > 40 else sub.sub_link or '-'}"
            )
            if sub.user_id:
                builder.button(
                    text=f"📋 Timeline کاربر",
                    callback_data=TimelineCallback(user_id=sub.user_id).pack(),
                )
    except (ValueError, AttributeError):
        pass

    # 4. Try as payment order_id string
    if not results:
        payment = await session.scalar(
            select(Payment).options(selectinload(Payment.user)).where(Payment.order_id == query)
        )
        if payment:
            u = payment.user
            results.append(
                f"💳 پرداخت (order_id): {str(payment.id)[:8]}\n"
                f"   {payment.price_amount}$ | {payment.payment_status} | {payment.kind}"
            )
            builder.button(
                text=f"🔍 پرداخت {str(payment.id)[:8]}",
                callback_data=RecoveryPaymentCallback(action="view", payment_id=payment.id).pack(),
            )

    # 5. Try as config name in subscriptions (via callback_payload)
    if not results:
        pay_result = await session.execute(
            select(Payment).options(selectinload(Payment.user))
            .where(Payment.callback_payload["config_name"].as_string() == query)
            .order_by(Payment.created_at.desc())
            .limit(5)
        )
        config_pays = list(pay_result.scalars().all())
        if config_pays:
            for cp in config_pays:
                u = cp.user
                results.append(
                    f"📛 کانفیگ '{query}': پرداخت {str(cp.id)[:8]}\n"
                    f"   {cp.price_amount}$ | {cp.payment_status} | کاربر: {u.first_name if u else '-'}"
                )
                builder.button(
                    text=f"🔍 {str(cp.id)[:8]}",
                    callback_data=RecoveryPaymentCallback(action="view", payment_id=cp.id).pack(),
                )

    # 6. Try partial first_name search as last resort
    if not results:
        user_result = await session.execute(
            select(User).options(selectinload(User.wallet))
            .where(func.lower(User.first_name).contains(q_clean.lower()))
            .limit(5)
        )
        found_users = list(user_result.scalars().all())
        for fu in found_users:
            balance = f"{fu.wallet.balance:.2f}" if fu.wallet else "0.00"
            results.append(
                f"👤 {fu.first_name or '-'} | @{fu.username or '-'} | TG: {fu.telegram_id} | ${balance}"
            )
            builder.button(
                text=f"📋 {fu.first_name or fu.telegram_id}",
                callback_data=TimelineCallback(user_id=fu.id).pack(),
            )

    if not results:
        text = f"🔍 نتیجه‌ای برای «{query}» یافت نشد."
    else:
        text = f"🔍 نتایج جستجو برای «{query}»:\n\n" + "\n\n".join(results)

    builder.button(text="🔍 جستجوی جدید", callback_data="admin:search")
    builder.button(text=AdminButtons.BACK, callback_data="admin:recovery")
    builder.adjust(1)

    await message.answer(text, reply_markup=builder.as_markup())
