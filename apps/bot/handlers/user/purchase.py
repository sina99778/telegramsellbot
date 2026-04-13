from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import build_plan_selection_keyboard, build_wallet_topup_keyboard
from core.formatting import format_volume_bytes
from core.qr import make_qr_bytes
from core.texts import Buttons, Messages
from models.order import Order
from models.plan import Plan
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.wallet.manager import InsufficientBalanceError, WalletManager


logger = logging.getLogger(__name__)

router = Router(name="user-purchase")


@router.callback_query(F.data == "pagination:noop")
async def ignore_pagination_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text == Buttons.BUY_CONFIG)
async def show_available_plans(message: Message, session: AsyncSession) -> None:
    result = await session.execute(
        select(Plan)
        .where(Plan.is_active.is_(True))
        .order_by(Plan.price.asc(), Plan.duration_days.asc())
    )
    plans = list(result.scalars().all())
    if not plans:
        await message.answer(Messages.NO_PLANS_AVAILABLE)
        return

    await message.answer(
        Messages.CHOOSE_PLAN,
        reply_markup=build_plan_selection_keyboard(plans),
    )


@router.callback_query(F.data.startswith("plan:select:"))
async def purchase_plan_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
) -> None:
    await callback.answer()
    if callback.from_user is None:
        return

    raw_plan_id = callback.data.rsplit(":", 1)[-1]
    try:
        plan_id = UUID(raw_plan_id)
    except ValueError:
        if callback.message is not None:
            await callback.message.answer("پلن انتخاب‌شده نامعتبر است.")
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or user.wallet is None or plan is None or not plan.is_active:
        if callback.message is not None:
            await callback.message.answer(Messages.PLAN_NOT_AVAILABLE)
        return

    if user.wallet.balance < plan.price:
        if callback.message is not None:
            await callback.message.answer(
                Messages.INSUFFICIENT_BALANCE.format(
                    balance=f"{user.wallet.balance:.2f}",
                    price=f"{plan.price:.2f}",
                    currency=plan.currency,
                ),
                reply_markup=build_wallet_topup_keyboard(),
            )
        return

    wallet_manager = WalletManager(session)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status="processing",
        source="bot",
        amount=plan.price,
        currency=plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(plan.price)),
            transaction_type="purchase",
            direction="debit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description=f"Purchase of plan {plan.code}",
            metadata={"plan_id": str(plan.id)},
        )
    except InsufficientBalanceError:
        order.status = "failed"
        if callback.message is not None:
            await callback.message.answer(Messages.BALANCE_NOT_SUFFICIENT_ANYMORE)
        return

    try:
        provisioning_manager = ProvisioningManager(session)
        provisioned = await provisioning_manager.provision_subscription(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
        )
    except ProvisioningError as exc:
        logger.error("Provisioning failed for order %s: %s", order.id, exc)
        try:
            await wallet_manager.process_transaction(
                user_id=user.id,
                amount=Decimal(str(plan.price)),
                transaction_type="refund",
                direction="credit",
                currency=plan.currency,
                reference_type="order",
                reference_id=order.id,
                description="Automatic refund after provisioning failure",
                metadata={"plan_id": str(plan.id)},
            )
            order.status = "refunded"
        except Exception as refund_exc:
            logger.critical(
                "CRITICAL: Refund also failed for order %s: %s", order.id, refund_exc
            )
            order.status = "failed_needs_manual_refund"
        if callback.message is not None:
            await callback.message.answer(Messages.PROVISIONING_FAILED_REFUNDED)
        return

    order.status = "provisioned"

    sub_link = provisioned.sub_link
    vless_uri = provisioned.vless_uri
    xui_record = provisioned.xui_client
    volume_label = format_volume_bytes(plan.volume_bytes)

    # ─── پیام اصلی ───────────────────────────────────────────────────
    text = (
        "✅ *کانفیگ شما آماده است\\!*\n\n"
        f"📦 پلن: *{_escape(plan.name)}*\n"
        f"💾 حجم: *{_escape(volume_label)}*\n"
        f"📅 مدت: *{plan.duration_days} روز*\n"
        f"🕐 فعال‌سازی: *از اولین اتصال*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🔗 *ساب لینک \\(برای وارد کردن در اپ\\):*\n"
        f"`{_escape(sub_link)}`\n\n"
        "📋 *کانفیگ مستقیم:*\n"
        f"`{_escape(vless_uri)}`\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📱 *QR Code رو اسکن کن یا کانفیگ بالا رو کپی کن*\n"
        "⚡ ساپورت اپ‌هایی مثل v2rayNG، Hiddify، NekoBox"
    )
    if callback.message is not None:
        await callback.message.answer(text, parse_mode="MarkdownV2")

    # ─── QR Code ──────────────────────────────────────────────────────
    qr_bytes = make_qr_bytes(vless_uri)
    if qr_bytes:
        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
            caption=f"📷 QR کد کانفیگ پلن *{_escape(plan.name)}*",
            parse_mode="MarkdownV2",
        )


def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))
