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
from core.formatting import format_volume_bytes, escape_markdown as _escape
from services.banner import create_traffic_banner
import urllib.parse
from core.texts import Buttons, Messages
from models.order import Order
from models.plan import Plan
from models.xui import XUIClientRecord
from repositories.discount import DiscountRepository
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.wallet.manager import InsufficientBalanceError, WalletManager
from apps.bot.utils.messaging import safe_edit_or_send


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
        await safe_edit_or_send(callback, "پلن انتخاب‌شده نامعتبر است.")
        return

    plan = await session.get(Plan, plan_id)
    if plan is None or not plan.is_active:
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    await state.update_data(plan_id=str(plan_id))
    await state.set_state(PurchaseStates.waiting_for_config_name)

    await safe_edit_or_send(callback, 
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
) -> None:
    """Skip discount and show payment method choice."""
    await callback.answer()
    await state.update_data(discount_code=None, discount_percent=0)
    await _show_payment_method_choice(callback, state, session)


@router.message(PurchaseStates.waiting_for_discount_code)
async def discount_code_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Validate discount code and show payment method choice."""
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
    await _show_payment_method_choice_msg(message, state, session)


async def _show_payment_method_choice(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show wallet vs gateway payment options (from callback)."""
    data = await state.get_data()
    plan = await session.get(Plan, UUID(data["plan_id"]))
    if plan is None:
        await state.clear()
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    discount_percent = data.get("discount_percent", 0)
    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    from core.formatting import format_price_with_toman
    from repositories.settings import AppSettingsRepository
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    price_display = format_price_with_toman(final_price, toman_rate)

    discount_line = ""
    if discount_percent > 0:
        orig_display = format_price_with_toman(original_price, toman_rate)
        discount_line = f"🏷 تخفیف: {discount_percent}% (قیمت اصلی: {orig_display})\n"

    text = (
        "💳 روش پرداخت را انتخاب کنید:\n\n"
        f"📦 پلن: {plan.name}\n"
        f"💰 مبلغ قابل پرداخت: {price_display}\n"
        f"{discount_line}\n"
        "از کدام روش پرداخت می‌خواهید استفاده کنید؟"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="👛 کیف پول", callback_data="purchase:pay:wallet")
    builder.button(text="💳 درگاه ریالی (تتراپی)", callback_data="purchase:pay:tetrapay")
    builder.button(text="💎 درگاه ارزی (NOWPayments)", callback_data="purchase:pay:gateway")
    builder.button(text=Buttons.BACK, callback_data="purchase:cancel")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


async def _show_payment_method_choice_msg(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show wallet vs gateway payment options (from message)."""
    data = await state.get_data()
    plan = await session.get(Plan, UUID(data["plan_id"]))
    if plan is None:
        await state.clear()
        await message.answer(Messages.PLAN_NOT_AVAILABLE)
        return

    discount_percent = data.get("discount_percent", 0)
    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    from core.formatting import format_price_with_toman
    from repositories.settings import AppSettingsRepository
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    price_display = format_price_with_toman(final_price, toman_rate)

    discount_line = ""
    if discount_percent > 0:
        orig_display = format_price_with_toman(original_price, toman_rate)
        discount_line = f"🏷 تخفیف: {discount_percent}% (قیمت اصلی: {orig_display})\n"

    text = (
        "💳 روش پرداخت را انتخاب کنید:\n\n"
        f"📦 پلن: {plan.name}\n"
        f"💰 مبلغ قابل پرداخت: {price_display}\n"
        f"{discount_line}\n"
        "از کدام روش پرداخت می‌خواهید استفاده کنید؟"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="👛 کیف پول", callback_data="purchase:pay:wallet")
    builder.button(text="💳 درگاه ریالی (تتراپی)", callback_data="purchase:pay:tetrapay")
    builder.button(text="💎 درگاه ارزی (NOWPayments)", callback_data="purchase:pay:gateway")
    builder.button(text=Buttons.BACK, callback_data="purchase:cancel")
    builder.adjust(1)

    await message.answer(text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "purchase:cancel")
async def cancel_purchase(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit_or_send(callback, Messages.CANCELLED)


@router.callback_query(F.data == "purchase:pay:wallet")
async def pay_with_wallet(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Pay with wallet balance."""
    await callback.answer()

    # Double-click prevention
    current_state = await state.get_state()
    if current_state == "purchase_processing":
        return
    await state.set_state("purchase_processing")

    # Typing indicator
    if callback.message:
        await bot.send_chat_action(callback.from_user.id, "typing")

    try:
        await _process_wallet_purchase(callback, state, session, bot)
    except Exception as exc:
        logger.error("Wallet purchase failed: %s", exc, exc_info=True)
        await state.clear()
        await safe_edit_or_send(callback, f"خطا در انجام خرید:\n{exc}")


@router.callback_query(F.data == "purchase:pay:gateway")
async def pay_with_gateway(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay with NowPayments gateway."""
    await callback.answer()
    try:
        await _process_gateway_purchase(callback, state, session)
    except Exception as exc:
        logger.error("Gateway purchase failed: %s", exc, exc_info=True)
        await state.clear()
        await safe_edit_or_send(callback, f"خطا در ساخت فاکتور:\n{exc}")


@router.callback_query(F.data == "purchase:pay:tetrapay")
async def pay_with_tetrapay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay with TetraPay gateway (Tomans)."""
    await callback.answer()
    try:
        await _process_tetrapay_purchase(callback, state, session)
    except Exception as exc:
        logger.error("TetraPay purchase failed: %s", exc, exc_info=True)
        await state.clear()
        await safe_edit_or_send(callback, f"❌ خطا در ایجاد فاکتور ریالی:\n<code>{exc}</code>", parse_mode="HTML")


async def _process_wallet_purchase(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Process purchase using wallet balance."""
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
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    # Calculate discounted price
    original_price = plan.price
    if discount_percent > 0:
        discounted = original_price * (Decimal(100 - discount_percent) / Decimal(100))
        final_price = discounted.quantize(Decimal("0.01"))
    else:
        final_price = original_price

    if user.wallet.balance < final_price:
        await safe_edit_or_send(callback, 
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
        bot=bot,
        session=session,
        user=user,
        plan=plan,
        final_price=final_price,
        original_price=original_price,
        discount_percent=discount_percent,
        config_name=config_name,
        payment_method="wallet",
    )


async def _process_gateway_purchase(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Create a NowPayments invoice for the purchase amount."""
    if callback.from_user is None:
        return

    data = await state.get_data()
    # DON'T clear state yet — we need it after payment confirmation via IPN

    plan_id = UUID(data["plan_id"])
    config_name = data.get("config_name", "VPN")
    discount_percent = data.get("discount_percent", 0)

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or plan is None or not plan.is_active:
        await state.clear()
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    from uuid import uuid4
    from core.config import settings
    from models.payment import Payment
    from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
    from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError

    local_order_id = str(uuid4())

    # Save purchase details in payment metadata so IPN can complete provisioning
    purchase_meta = {
        "plan_id": str(plan_id),
        "config_name": config_name,
        "discount_percent": discount_percent,
        "discount_id": data.get("discount_id"),
        "purpose": "direct_purchase",
    }

    payload = NowPaymentsPaymentCreateRequest(
        price_amount=final_price,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Purchase plan {plan.name} for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )

    try:
        async with NowPaymentsClient(
            NowPaymentsClientConfig(
                api_key=settings.nowpayments_api_key,
                base_url=settings.nowpayments_base_url,
            )
        ) as client:
            invoice = await client.create_payment_invoice(payload)
    except NowPaymentsRequestError:
        await state.clear()
        await safe_edit_or_send(callback, Messages.PAYMENT_GATEWAY_UNAVAILABLE)
        return

    payment = Payment(
        user_id=user.id,
        provider="nowpayments",
        kind="direct_purchase",
        provider_payment_id=None,
        provider_invoice_id=str(invoice.id),
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency=None,
        price_currency="USD",
        price_amount=final_price,
        invoice_url=str(invoice.invoice_url),
        callback_payload=purchase_meta,
    )
    session.add(payment)
    await session.flush()
    await state.clear()

    from apps.bot.keyboards.inline import build_topup_link_keyboard


    discount_line = ""
    if discount_percent > 0:
        discount_line = f"🏷 تخفیف: {discount_percent}%\n"

    await safe_edit_or_send(
        callback,
        f"🧾 فاکتور خرید ساخته شد:\n\n"
        f"📦 پلن: {plan.name}\n"
        f"💰 مبلغ: {final_price} USD\n"
        f"{discount_line}\n"
        "بعد از پرداخت و تایید NOWPayments، کانفیگ شما "
        "به صورت خودکار ساخته و ارسال می‌شود.",
        reply_markup=build_topup_link_keyboard(str(invoice.invoice_url)),
    )


async def _process_tetrapay_purchase(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Create a TetraPay invoice for the purchase amount in Tomans."""
    if callback.from_user is None:
        return

    data = await state.get_data()
    # DON'T clear state yet
    
    plan_id = UUID(data["plan_id"])
    config_name = data.get("config_name", "VPN")
    discount_percent = data.get("discount_percent", 0)

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or plan is None or not plan.is_active:
        await state.clear()
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    from repositories.settings import AppSettingsRepository
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        await state.clear()
        await safe_edit_or_send(callback, "❌ نرخ تبدیل تومان تنظیم نشده. لطفاً با پشتیبانی تماس بگیرید.")
        return

    # Cost in tomans
    toman_amount = int((final_price * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10
    
    # TetraPay minimum amount check (10,000 Rials = 1,000 Tomans)
    if rial_amount < 10000:
        await state.clear()
        await safe_edit_or_send(
            callback,
            f"❌ مبلغ پرداخت ({toman_amount:,} تومان) کمتر از حداقل مبلغ مجاز درگاه (۱,۰۰۰ تومان) است.\n"
            "لطفاً از کیف پول یا درگاه ارزی استفاده کنید."
        )
        return

    # TetraPay maximum amount check
    from core.config import settings as app_settings
    max_toman = app_settings.tetrapay_max_amount_toman
    if toman_amount > max_toman:
        await state.clear()
        await safe_edit_or_send(
            callback,
            f"❌ مبلغ پرداخت ({toman_amount:,} تومان) بیشتر از سقف مجاز درگاه تتراپی ({max_toman:,} تومان) است.\n\n"
            "💡 راه‌حل:\n"
            "• از **درگاه ارزی (NOWPayments)** استفاده کنید\n"
            "• یا ابتدا **کیف پول** خود را شارژ کنید و سپس با کیف پول خرید کنید"
        )
        return

    logger.info(
        "TetraPay purchase: user=%s, plan=%s, price_usd=%s, toman=%s, rial=%s",
        user.telegram_id, plan.name, final_price, toman_amount, rial_amount
    )
    
    from uuid import uuid4
    from core.config import settings
    from models.payment import Payment
    from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

    local_order_id = str(uuid4())

    purchase_meta = {
        "plan_id": str(plan_id),
        "config_name": config_name,
        "discount_percent": discount_percent,
        "discount_id": data.get("discount_id"),
        "purpose": "direct_purchase",
    }
    
    try:
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=settings.tetrapay_api_key.get_secret_value(),
                base_url=settings.tetrapay_base_url,
            )
        ) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"خرید سرویس {plan.name} - کاربر {user.telegram_id}",
                email=f"{user.telegram_id}@telegram.org",
                mobile="09111111111",
            )
    except TetraPayRequestError as exc:
        logger.error("TetraPay create_order failed for user %s: %s", user.telegram_id, exc)
        await state.clear()
        await safe_edit_or_send(
            callback,
            f"❌ خطا در ساخت فاکتور تتراپی:\n<code>{exc}</code>\n\n"
            "لطفاً دوباره تلاش کنید یا از روش پرداخت دیگری استفاده کنید.",
            parse_mode="HTML"
        )
        return

    payment = Payment(
        user_id=user.id,
        provider="tetrapay",
        kind="direct_purchase",
        provider_payment_id=tx.Authority,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=final_price,
        pay_amount=toman_amount,
        invoice_url=tx.payment_url_bot,
        callback_payload=purchase_meta,
    )
    session.add(payment)
    await session.flush()
    await state.clear()

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    from core.formatting import format_price_with_toman



    price_display = format_price_with_toman(final_price, toman_rate)

    text = (
        "🔖 **فاکتور پرداخت (ریالی/تومانی)**\n\n"
        f"💳 درگاه: تتراپی\n"
        f"💵 مبلغ پرداخت: `{toman_amount:,}` تومان\n\n"
        "👇 برای پرداخت روی دکمه زیر کلیک کنید:"
    )
    await safe_edit_or_send(
        callback, text, reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_bot, bot_url=tx.payment_url_bot)
    )




async def _finalize_purchase(
    *,
    chat_id: int,
    bot: Bot,
    session: AsyncSession,
    user,
    plan: Plan,
    final_price: Decimal,
    original_price: Decimal,
    discount_percent: int,
    config_name: str,
    payment_method: str = "wallet",
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
        await bot.send_message(chat_id=chat_id, text=Messages.BALANCE_NOT_SUFFICIENT_ANYMORE)
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
        await bot.send_message(chat_id=chat_id, text=Messages.PROVISIONING_FAILED_REFUNDED)
        return

    order.status = "provisioned"

    sub_link = provisioned.sub_link
    vless_uri = provisioned.vless_uri
    volume_label = format_volume_bytes(plan.volume_bytes)

    # Build message
    discount_line = ""
    if discount_percent > 0:
        discount_line = f"🏷 تخفیف: *{discount_percent}%* \\(قیمت اصلی: {_escape(str(original_price))}\\)\n"

    payment_label = "کیف پول" if payment_method == "wallet" else "درگاه پرداخت"

    text = (
        "✅ *کانفیگ شما آماده است\\!*\n\n"
        f"📛 نام: *{_escape(config_name)}*\n"
        f"📦 پلن: *{_escape(plan.name)}*\n"
        f"💾 حجم: *{_escape(volume_label)}*\n"
        f"📅 مدت: *{plan.duration_days} روز*\n"
        f"💰 پرداخت شده: *{_escape(str(final_price))} {_escape(plan.currency)}*\n"
        f"💳 روش پرداخت: *{_escape(payment_label)}*\n"
        f"{discount_line}"
        f"🕐 فعال‌سازی: *از اولین اتصال*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🔗 *ساب لینک \\(برای وارد کردن در اپ\\):*\n"
        f"`{_escape(sub_link)}`\n\n"
        "📋 *کانفیگ مستقیم:*\n"
        f"`{_escape(vless_uri)}`\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "⚡ برای اتصال سریع روی دکمه‌های زیر کلیک کنید"
    )

    builder = InlineKeyboardBuilder()
    if vless_uri:
        from core.config import settings
        base = settings.web_base_url.rstrip("/")
        encoded_uri = urllib.parse.quote(vless_uri, safe="")
        builder.button(text="🟢 اتصال v2rayNG", url=f"{base}/api/dl/v2rayng?url={encoded_uri}")
        builder.button(text="🍎 اتصال Shadowrocket", url=f"{base}/api/dl/shadowrocket?url={encoded_uri}")
        builder.button(text="🍎 اتصال V2Box", url=f"{base}/api/dl/v2box?url={encoded_uri}")
        builder.adjust(2)

    # Generate Banner
    banner_bytes = create_traffic_banner(
        config_name=config_name,
        user_id=user.id,
        status="pending_activation",
        used_gb=0.0,
        total_gb=plan.volume_bytes / (1024**3),
        days_left=plan.duration_days,
        is_active=True
    )
    
    if banner_bytes:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(banner_bytes.getvalue(), filename="banner.png"),
            caption=text,
            reply_markup=builder.as_markup() if vless_uri else None,
            parse_mode="MarkdownV2"
        )
    else:
        await bot.send_message(
            chat_id=chat_id, 
            text=text, 
            reply_markup=builder.as_markup() if vless_uri else None,
            parse_mode="MarkdownV2"
        )

    # ── Notify admins about the purchase ──
    from services.notifications import notify_admins

    user_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.telegram_id}'>مشاهده پروفایل</a>"
    admin_text = (
        "🛒 خرید جدید!\n\n"
        f"👤 کاربر: {user.first_name or '-'} | {user_link} (ID: <code>{user.telegram_id}</code>)\n"
        f"📦 پلن: {plan.name}\n"
        f"💰 مبلغ: {final_price} {plan.currency}\n"
        f"📛 کانفیگ: {config_name}\n"
        f"💳 روش: {payment_label}"
    )
    try:
        await notify_admins(session, bot, admin_text)
    except Exception as exc:
        logger.warning("Failed to notify admins about purchase: %s", exc)

    # ── Referral bonus on first purchase ──
    try:
        await _process_referral_bonus(session, bot, user)
    except Exception as exc:
        logger.warning("Failed to process referral bonus: %s", exc)


async def _process_referral_bonus(session, bot, user) -> None:
    """Credit referral bonus to both referrer and referee on first purchase."""
    from sqlalchemy import func, select as sel
    from repositories.settings import AppSettingsRepository
    from services.wallet.manager import WalletManager

    settings_repo = AppSettingsRepository(session)
    ref_settings = await settings_repo.get_referral_settings()

    if not ref_settings.enabled:
        return

    if user.referred_by_user_id is None:
        return

    # Check if this is the user's first completed order
    order_count = int(
        await session.scalar(
            sel(func.count()).select_from(Order)
            .where(
                Order.user_id == user.id,
                Order.status.in_(["provisioned", "paid", "completed"]),
            )
        ) or 0
    )
    if order_count != 1:
        # Only give bonus on the very first purchase
        return

    wallet_manager = WalletManager(session)

    # Credit referrer
    if ref_settings.referrer_bonus_usd > 0:
        await wallet_manager.process_transaction(
            user_id=user.referred_by_user_id,
            amount=Decimal(str(ref_settings.referrer_bonus_usd)),
            transaction_type="referral_bonus",
            direction="credit",
            currency="USD",
            reference_type="referral",
            reference_id=user.id,
            description=f"Referral bonus for inviting user {user.telegram_id}",
            metadata={"referred_user_id": str(user.id), "referred_telegram_id": user.telegram_id},
        )
        # Notify referrer
        try:
            from models.user import User as UserModel
            referrer = await session.get(UserModel, user.referred_by_user_id)
            if referrer:
                await bot.send_message(
                    referrer.telegram_id,
                    f"🎉 تبریک! کاربری که دعوت کرده بودید اولین خرید خود را انجام داد.\n"
                    f"💰 {ref_settings.referrer_bonus_usd:.2f} دلار به کیف پول شما اضافه شد!",
                )
        except Exception as exc:
            logger.warning("Failed to notify referrer: %s", exc)

    # Credit referee (the buying user)
    if ref_settings.referee_bonus_usd > 0:
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(ref_settings.referee_bonus_usd)),
            transaction_type="referral_bonus",
            direction="credit",
            currency="USD",
            reference_type="referral",
            reference_id=user.referred_by_user_id,
            description="Referral welcome bonus",
            metadata={"referrer_user_id": str(user.referred_by_user_id)},
        )
        try:
            await bot.send_message(
                user.telegram_id,
                f"🎁 خوش آمدید! به خاطر عضویت از طریق لینک دعوت، "
                f"{ref_settings.referee_bonus_usd:.2f} دلار به کیف پول شما اضافه شد!",
            )
        except Exception as exc:
            logger.warning("Failed to notify referee: %s", exc)
