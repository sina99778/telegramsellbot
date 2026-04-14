from __future__ import annotations

import logging
import re
from decimal import Decimal
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import build_plan_selection_keyboard, build_wallet_topup_keyboard
from apps.bot.states.purchase import PurchaseStates
from core.formatting import format_volume_bytes
from core.qr import make_qr_bytes
from core.texts import Buttons, Messages
from models.order import Order
from models.plan import Plan
from models.xui import XUIClientRecord
from repositories.discount import DiscountRepository
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.wallet.manager import InsufficientBalanceError, WalletManager


logger = logging.getLogger(__name__)

router = Router(name="user-purchase")

# Allowed config name pattern: letters, digits, underscores, dashes, 3-32 chars
CONFIG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{3,32}$")


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
async def plan_selected_ask_name(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """After selecting a plan, ask the user for a config name."""
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

    plan = await session.get(Plan, plan_id)
    if plan is None or not plan.is_active:
        if callback.message is not None:
            await callback.message.answer(Messages.PLAN_NOT_AVAILABLE)
        return

    await state.update_data(plan_id=str(plan_id))
    await state.set_state(PurchaseStates.waiting_for_config_name)

    if callback.message is not None:
        await callback.message.answer(
            "📝 لطفاً یک نام برای کانفیگ خود انتخاب کنید:\n\n"
            "• فقط حروف انگلیسی، اعداد، خط تیره و آندرلاین مجاز است\n"
            "• طول نام بین ۳ تا ۳۲ کاراکتر باشد\n"
            "• مثال: `MyVPN` یا `phone-1`"
        )


@router.message(PurchaseStates.waiting_for_config_name)
async def config_name_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Validate config name uniqueness."""
    if not message.text:
        return

    config_name = message.text.strip()

    if not CONFIG_NAME_PATTERN.match(config_name):
        await message.answer(
            "❌ نام نامعتبر است.\n"
            "فقط حروف انگلیسی، اعداد، خط تیره و آندرلاین (۳ تا ۳۲ کاراکتر) مجاز است.\n"
            "لطفاً نام دیگری وارد کنید:"
        )
        return

    # Check uniqueness: the config name will be used as the remark in X-UI
    existing = await session.scalar(
        select(XUIClientRecord).where(XUIClientRecord.username == config_name)
    )
    if existing:
        await message.answer(
            "⚠️ این نام قبلاً استفاده شده. لطفاً نام دیگری انتخاب کنید:"
        )
        return

    await state.update_data(config_name=config_name)
    await state.set_state(PurchaseStates.waiting_for_discount_code)

    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ بدون کد تخفیف", callback_data="purchase:skip_discount")
    builder.adjust(1)

    await message.answer(
        "🏷 اگر کد تخفیف دارید وارد کنید.\n"
        "در غیر این صورت دکمه زیر را بزنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "purchase:skip_discount")
async def skip_discount_code(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Skip discount and proceed to payment."""
    await callback.answer()
    await state.update_data(discount_code=None, discount_percent=0)
    await _process_purchase(callback, state, session, bot)


@router.message(PurchaseStates.waiting_for_discount_code)
async def discount_code_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Validate discount code and proceed."""
    if not message.text:
        return

    code = message.text.strip().upper()
    data = await state.get_data()
    plan_id = UUID(data["plan_id"])

    repo = DiscountRepository(session)
    discount = await repo.validate_code(code, plan_id=plan_id)

    if discount is None:
        await message.answer(
            "❌ کد تخفیف نامعتبر، منقضی شده یا قابل استفاده نیست.\n"
            "لطفاً کد دیگری وارد کنید یا بدون تخفیف ادامه دهید."
        )
        return

    await state.update_data(
        discount_code=discount.code,
        discount_percent=discount.discount_percent,
        discount_id=str(discount.id),
    )
    
    # Create a fake callback to reuse _process_purchase
    # Actually, we'll call the processing directly
    await _process_purchase_from_message(message, state, session, bot)


async def _process_purchase(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Core purchase logic triggered from callback."""
    if callback.from_user is None:
        return

    data = await state.get_data()
    await state.clear()

    plan_id = UUID(data["plan_id"])
    config_name = data.get("config_name", "VPN")
    discount_percent = data.get("discount_percent", 0)
    discount_id = data.get("discount_id")

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or user.wallet is None or plan is None or not plan.is_active:
        if callback.message is not None:
            await callback.message.answer(Messages.PLAN_NOT_AVAILABLE)
        return

    # Calculate discounted price
    original_price = plan.price
    if discount_percent > 0:
        discounted = original_price * (Decimal(100 - discount_percent) / Decimal(100))
        final_price = discounted.quantize(Decimal("0.01"))
    else:
        final_price = original_price

    if user.wallet.balance < final_price:
        if callback.message is not None:
            await callback.message.answer(
                Messages.INSUFFICIENT_BALANCE.format(
                    balance=f"{user.wallet.balance:.2f}",
                    price=f"{final_price:.2f}",
                    currency=plan.currency,
                ),
                reply_markup=build_wallet_topup_keyboard(),
            )
        return

    # Use the discount code
    if discount_id:
        repo = DiscountRepository(session)
        from models.discount import DiscountCode
        dc = await session.get(DiscountCode, UUID(discount_id))
        if dc:
            await repo.use_code(dc)

    await _finalize_purchase(
        chat_id=callback.from_user.id,
        message_obj=callback.message,
        bot=bot,
        session=session,
        user=user,
        plan=plan,
        final_price=final_price,
        original_price=original_price,
        discount_percent=discount_percent,
        config_name=config_name,
    )


async def _process_purchase_from_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Core purchase logic triggered from message (discount code entry)."""
    if message.from_user is None:
        return

    data = await state.get_data()
    await state.clear()

    plan_id = UUID(data["plan_id"])
    config_name = data.get("config_name", "VPN")
    discount_percent = data.get("discount_percent", 0)
    discount_id = data.get("discount_id")

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or user.wallet is None or plan is None or not plan.is_active:
        await message.answer(Messages.PLAN_NOT_AVAILABLE)
        return

    original_price = plan.price
    if discount_percent > 0:
        discounted = original_price * (Decimal(100 - discount_percent) / Decimal(100))
        final_price = discounted.quantize(Decimal("0.01"))
    else:
        final_price = original_price

    if user.wallet.balance < final_price:
        await message.answer(
            Messages.INSUFFICIENT_BALANCE.format(
                balance=f"{user.wallet.balance:.2f}",
                price=f"{final_price:.2f}",
                currency=plan.currency,
            ),
            reply_markup=build_wallet_topup_keyboard(),
        )
        return

    if discount_id:
        repo = DiscountRepository(session)
        from models.discount import DiscountCode
        dc = await session.get(DiscountCode, UUID(discount_id))
        if dc:
            await repo.use_code(dc)

    await _finalize_purchase(
        chat_id=message.from_user.id,
        message_obj=message,
        bot=bot,
        session=session,
        user=user,
        plan=plan,
        final_price=final_price,
        original_price=original_price,
        discount_percent=discount_percent,
        config_name=config_name,
    )


async def _finalize_purchase(
    *,
    chat_id: int,
    message_obj,
    bot: Bot,
    session: AsyncSession,
    user,
    plan: Plan,
    final_price: Decimal,
    original_price: Decimal,
    discount_percent: int,
    config_name: str,
) -> None:
    """Shared purchase finalization: wallet debit, provisioning, sending config."""
    wallet_manager = WalletManager(session)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status="processing",
        source="bot",
        amount=final_price,
        currency=plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(final_price)),
            transaction_type="purchase",
            direction="debit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description=f"Purchase of plan {plan.code}",
            metadata={"plan_id": str(plan.id), "config_name": config_name},
        )
    except InsufficientBalanceError:
        order.status = "failed"
        await message_obj.answer(Messages.BALANCE_NOT_SUFFICIENT_ANYMORE)
        return

    try:
        provisioning_manager = ProvisioningManager(session)
        provisioned = await provisioning_manager.provision_subscription(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
            config_name=config_name,
        )
    except ProvisioningError as exc:
        logger.error("Provisioning failed for order %s: %s", order.id, exc)
        try:
            await wallet_manager.process_transaction(
                user_id=user.id,
                amount=Decimal(str(final_price)),
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
        await message_obj.answer(Messages.PROVISIONING_FAILED_REFUNDED)
        return

    order.status = "provisioned"

    sub_link = provisioned.sub_link
    vless_uri = provisioned.vless_uri
    volume_label = format_volume_bytes(plan.volume_bytes)

    # Build message
    discount_line = ""
    if discount_percent > 0:
        discount_line = f"🏷 تخفیف: *{discount_percent}%* \\(قیمت اصلی: {_escape(str(original_price))}\\)\n"

    text = (
        "✅ *کانفیگ شما آماده است\\!*\n\n"
        f"📛 نام: *{_escape(config_name)}*\n"
        f"📦 پلن: *{_escape(plan.name)}*\n"
        f"💾 حجم: *{_escape(volume_label)}*\n"
        f"📅 مدت: *{plan.duration_days} روز*\n"
        f"💰 پرداخت شده: *{_escape(str(final_price))} {_escape(plan.currency)}*\n"
        f"{discount_line}"
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
    await message_obj.answer(text, parse_mode="MarkdownV2")

    # QR Code
    qr_bytes = make_qr_bytes(vless_uri)
    if qr_bytes:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
            caption=f"📷 QR کد کانفیگ *{_escape(config_name)}*",
            parse_mode="MarkdownV2",
        )


def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))
