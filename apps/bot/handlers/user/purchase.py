from __future__ import annotations

import logging
import re
from decimal import Decimal
from uuid import UUID, uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import build_plan_selection_keyboard, build_wallet_topup_keyboard
from apps.bot.states.purchase import PurchaseStates
from core.formatting import format_volume_bytes, escape_markdown as _escape
from services.banner import create_traffic_banner
import urllib.parse
from core.texts import Buttons, Messages
from models.order import Order
from models.payment import Payment
from models.plan import Plan
from models.xui import XUIClientRecord
from repositories.discount import DiscountRepository
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.custom_purchase import (
    CustomPurchaseError,
    calculate_custom_purchase_price,
    create_custom_purchase_plan,
    get_custom_purchase_template_plan,
)
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.plan_inventory import ensure_plan_available, get_effective_plan_stock_map, is_stock_available, PlanStockError
from services.phone_verification import get_verified_phone, is_valid_phone_number, normalize_phone_number, set_verified_phone
from services.wallet.manager import InsufficientBalanceError, WalletManager
from apps.bot.utils.messaging import safe_edit_or_send


logger = logging.getLogger(__name__)

router = Router(name="user-purchase")

# Allowed config name pattern: letters, digits, underscores, dashes, 3-32 chars
CONFIG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{3,32}$")


def _phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="ارسال شماره موبایل", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _ensure_phone_verified_for_purchase(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> bool:
    if message.from_user is None:
        return False

    repo = AppSettingsRepository(session)
    settings = await repo.get_phone_verification_settings()
    if not settings.enabled:
        return True

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is not None and get_verified_phone(user):
        return True

    await state.set_state(PurchaseStates.waiting_for_phone_verification)
    hint = "شماره موبایل ایران" if settings.mode == "iran" else "شماره موبایل"
    await message.answer(
        f"برای ادامه خرید، لطفا {hint} خود را ارسال کنید.",
        reply_markup=_phone_request_keyboard(),
    )
    return False


@router.callback_query(F.data == "pagination:noop")
async def ignore_pagination_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text == Buttons.BUY_CONFIG)
async def show_available_plans(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not await _ensure_phone_verified_for_purchase(message, session, state):
        return

    result = await session.execute(
        select(Plan)
        .where(Plan.is_active.is_(True), not_(Plan.code.like("custom\\_%", escape="\\")))
        .order_by(Plan.price.asc(), Plan.duration_days.asc())
    )
    plans = list(result.scalars().all())
    stock_by_plan_id = await get_effective_plan_stock_map(session, [plan.id for plan in plans])
    plans = [plan for plan in plans if is_stock_available(stock_by_plan_id[plan.id])]
    custom_settings = await AppSettingsRepository(session).get_custom_purchase_settings()
    custom_available = bool(
        custom_settings.enabled
        and custom_settings.price_per_gb > 0
        and custom_settings.price_per_day > 0
        and await get_custom_purchase_template_plan(session)
    )
    if not plans and not custom_available:
        await message.answer(Messages.NO_PLANS_AVAILABLE)
        return

    await message.answer(
        Messages.CHOOSE_PLAN,
        reply_markup=build_plan_selection_keyboard(
            plans,
            stock_by_plan_id,
            include_custom_purchase=custom_available,
        ),
    )


@router.message(PurchaseStates.waiting_for_phone_verification)
async def phone_verification_submitted(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if message.from_user is None:
        return

    # Only accept contact button — reject plain text phone numbers
    if not message.contact:
        await message.answer(
            "لطفا از دکمه «ارسال شماره موبایل» استفاده کنید.",
            reply_markup=_phone_request_keyboard(),
        )
        return

    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("لطفا شماره موبایل خودتان را ارسال کنید.")
        return

    phone = normalize_phone_number(message.contact.phone_number)
    settings = await AppSettingsRepository(session).get_phone_verification_settings()
    if not is_valid_phone_number(phone, settings.mode):
        hint = "یک شماره ایران معتبر مثل 09123456789" if settings.mode == "iran" else "یک شماره معتبر"
        await message.answer(f"شماره معتبر نیست. لطفا {hint} ارسال کنید.")
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer(Messages.ACCOUNT_NOT_FOUND, reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    await set_verified_phone(session, user, phone)
    await state.clear()
    await message.answer(
        "✅ شماره موبایل تایید شد.",
        reply_markup=ReplyKeyboardRemove(),
    )
    # Automatically continue to the purchase flow
    await show_available_plans(message, session, state)


@router.callback_query(F.data == "purchase:custom")
async def custom_purchase_volume_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    settings = await AppSettingsRepository(session).get_custom_purchase_settings()
    template = await get_custom_purchase_template_plan(session)
    if not settings.enabled or settings.price_per_gb <= 0 or settings.price_per_day <= 0 or template is None:
        await safe_edit_or_send(callback, "خرید دلخواه فعلاً در دسترس نیست.")
        return

    await state.clear()
    await state.update_data(custom_purchase=True)
    await state.set_state(PurchaseStates.waiting_for_custom_volume)
    await safe_edit_or_send(
        callback,
        "🧩 خرید دلخواه\n\n"
        f"قیمت هر ۱ گیگ: {settings.price_per_gb} دلار\n"
        f"قیمت هر ۱ روز: {settings.price_per_day} دلار\n\n"
        "حجم موردنظر را به گیگابایت وارد کنید. مثال: 25",
    )


@router.message(PurchaseStates.waiting_for_custom_volume)
async def custom_purchase_volume_entered(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        volume_gb = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("حجم معتبر نیست. مثال: 25")
        return
    if volume_gb <= 0:
        await message.answer("حجم باید بیشتر از صفر باشد.")
        return
    await state.update_data(custom_volume_gb=volume_gb)
    await state.set_state(PurchaseStates.waiting_for_custom_days)
    await message.answer("مدت موردنظر را به روز وارد کنید. مثال: 30")


@router.message(PurchaseStates.waiting_for_custom_days)
async def custom_purchase_days_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return
    try:
        duration_days = int(message.text.strip().replace(",", ""))
    except ValueError:
        await message.answer("مدت معتبر نیست. مثال: 30")
        return
    if duration_days <= 0:
        await message.answer("مدت باید بیشتر از صفر باشد.")
        return

    data = await state.get_data()
    volume_gb = float(data.get("custom_volume_gb") or 0)
    settings = await AppSettingsRepository(session).get_custom_purchase_settings()
    template = await get_custom_purchase_template_plan(session)
    if template is None:
        await state.clear()
        await message.answer("برای خرید دلخواه حداقل یک پلن فعال متصل به سرور لازم است.")
        return

    try:
        price = calculate_custom_purchase_price(
            settings,
            volume_gb=volume_gb,
            duration_days=duration_days,
        )
        custom_plan = await create_custom_purchase_plan(
            session,
            volume_gb=volume_gb,
            duration_days=duration_days,
            price=price,
            template_plan=template,
        )
    except CustomPurchaseError as exc:
        await state.clear()
        await message.answer(str(exc))
        return

    await state.update_data(
        plan_id=str(custom_plan.id),
        custom_duration_days=duration_days,
    )
    await state.set_state(PurchaseStates.waiting_for_config_name)
    await message.answer(
        f"خرید دلخواه آماده شد:\n"
        f"حجم: {volume_gb:g} GB\n"
        f"مدت: {duration_days} روز\n"
        f"قیمت: {price} USD\n\n"
        "حالا یک نام برای کانفیگ وارد کنید. فقط حروف انگلیسی، عدد، خط تیره و آندرلاین.",
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
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
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
    # Apply personal discount if user has one
    if callback.from_user:
        from repositories.user import UserRepository
        user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        personal_pct = getattr(user, "personal_discount_percent", 0) if user else 0
    else:
        personal_pct = 0
    await state.update_data(discount_code=None, discount_percent=personal_pct)
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

    # Take the better of: personal discount vs code discount
    if message.from_user:
        from repositories.user import UserRepository
        user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
        personal_pct = getattr(user, "personal_discount_percent", 0) if user else 0
    else:
        personal_pct = 0

    effective_percent = max(discount.discount_percent, personal_pct)

    await state.update_data(
        discount_code=discount.code,
        discount_percent=effective_percent,
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
    settings_repo = AppSettingsRepository(session)
    toman_rate = await settings_repo.get_toman_rate()
    gw = await settings_repo.get_gateway_settings()
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
    if gw.tetrapay_enabled:
        builder.button(text="💳 درگاه ریالی (تتراپی)", callback_data="purchase:pay:tetrapay")
    if gw.tronado_enabled:
        builder.button(text="درگاه ترونادو", callback_data="purchase:pay:tronado")
    if gw.nowpayments_enabled:
        builder.button(text="💎 درگاه ارزی (NOWPayments)", callback_data="purchase:pay:gateway")
    if gw.manual_crypto_enabled and (gw.manual_crypto_wallets or gw.manual_crypto_address):
        builder.button(text="💰 پرداخت به ولت (دستی)", callback_data="purchase:pay:manual")
    if gw.card_to_card_enabled and gw.card_number and gw.card_holder:
        builder.button(text="کارت به کارت", callback_data="purchase:pay:card")
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
    settings_repo = AppSettingsRepository(session)
    toman_rate = await settings_repo.get_toman_rate()
    gw = await settings_repo.get_gateway_settings()
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
    if gw.tetrapay_enabled:
        builder.button(text="💳 درگاه ریالی (تتراپی)", callback_data="purchase:pay:tetrapay")
    if gw.tronado_enabled:
        builder.button(text="درگاه ترونادو", callback_data="purchase:pay:tronado")
    if gw.nowpayments_enabled:
        builder.button(text="💎 درگاه ارزی (NOWPayments)", callback_data="purchase:pay:gateway")
    if gw.manual_crypto_enabled and (gw.manual_crypto_wallets or gw.manual_crypto_address):
        builder.button(text="💰 پرداخت به ولت (دستی)", callback_data="purchase:pay:manual")
    if gw.card_to_card_enabled and gw.card_number and gw.card_holder:
        builder.button(text="کارت به کارت", callback_data="purchase:pay:card")
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

    # Double-click prevention
    current_state = await state.get_state()
    if current_state == "purchase_processing":
        return
    await state.set_state("purchase_processing")

    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.nowpayments_enabled:
        await state.clear()
        await safe_edit_or_send(callback, "❌ درگاه ارزی غیرفعال است.")
        return
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

    # Double-click prevention
    current_state = await state.get_state()
    if current_state == "purchase_processing":
        return
    await state.set_state("purchase_processing")

    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.tetrapay_enabled:
        await state.clear()
        await safe_edit_or_send(callback, "❌ درگاه ریالی غیرفعال است.")
        return
    try:
        await _process_tetrapay_purchase(callback, state, session)
    except Exception as exc:
        logger.error("TetraPay purchase failed: %s", exc, exc_info=True)
        await state.clear()
        await safe_edit_or_send(callback, f"❌ خطا در ایجاد فاکتور ریالی:\n<code>{exc}</code>", parse_mode="HTML")


@router.callback_query(F.data == "purchase:pay:tronado")
async def pay_with_tronado(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()

    current_state = await state.get_state()
    if current_state == "purchase_processing":
        return
    if current_state is None:
        await safe_edit_or_send(callback, "درخواست قبلا پردازش شده یا منقضی شده است.")
        return
    await state.set_state("purchase_processing")

    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.tronado_enabled:
        await state.clear()
        await safe_edit_or_send(callback, "درگاه ترونادو غیرفعال است.")
        return

    try:
        await _process_tronado_purchase(callback, state, session)
    except Exception as exc:
        logger.error("Tronado purchase failed: %s", exc, exc_info=True)
        await state.clear()
        await safe_edit_or_send(callback, f"خطا در ایجاد فاکتور ترونادو:\n<code>{exc}</code>", parse_mode="HTML")


@router.callback_query(F.data.startswith("purchase:pay:manual"))
async def pay_with_manual_crypto(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Redirect to manual crypto topup for the purchase amount, then user pays from wallet."""
    await callback.answer()

    data = await state.get_data()
    plan_id_str = data.get("plan_id")
    if not plan_id_str:
        await safe_edit_or_send(callback, "❌ اطلاعات پلن یافت نشد.")
        return

    plan = await session.get(Plan, UUID(plan_id_str))
    if plan is None or not plan.is_active:
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
        return

    discount_percent = data.get("discount_percent", 0)
    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    # Store the topup amount in state and redirect to manual crypto handler
    await state.update_data(topup_amount=str(final_price))

    # Import and call the manual crypto handler directly
    from apps.bot.handlers.user.topup import topup_pay_manual
    await topup_pay_manual(callback, state, session)


@router.callback_query(F.data == "purchase:pay:card")
async def pay_with_card_to_card(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    if callback.from_user is None:
        return

    data = await state.get_data()
    plan_id_str = data.get("plan_id")
    if not plan_id_str:
        await safe_edit_or_send(callback, "اطلاعات خرید پیدا نشد. لطفا دوباره تلاش کنید.")
        return

    plan = await session.get(Plan, UUID(plan_id_str))
    if plan is None or not plan.is_active:
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
        return

    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    if not gw.card_to_card_enabled or not gw.card_number or not gw.card_holder:
        await safe_edit_or_send(callback, "پرداخت کارت به کارت در حال حاضر فعال نیست.")
        return

    discount_percent = int(data.get("discount_percent", 0) or 0)
    original_price = plan.price
    final_price = (
        original_price * (Decimal(100 - discount_percent) / Decimal(100))
    ).quantize(Decimal("0.01")) if discount_percent > 0 else original_price
    toman_rate = await settings_repo.get_toman_rate()
    toman_amount = int((final_price * toman_rate).quantize(Decimal("1")))

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, Messages.ACCOUNT_NOT_FOUND)
        return

    payment = Payment(
        user_id=user.id,
        provider="card_to_card",
        kind="direct_purchase",
        order_id=str(uuid4()),
        payment_status="waiting_receipt",
        pay_currency="IRT",
        price_currency=plan.currency,
        price_amount=final_price,
        pay_amount=Decimal(toman_amount),
        callback_payload={
            "plan_id": str(plan.id),
            "config_name": data.get("config_name", "VPN"),
            "discount_percent": discount_percent,
            "discount_id": data.get("discount_id"),
            "purpose": "direct_purchase",
            "card_number": gw.card_number,
            "card_holder": gw.card_holder,
            "card_bank": gw.card_bank,
        },
    )
    session.add(payment)
    await session.flush()

    await state.update_data(card_payment_id=str(payment.id))
    await state.set_state(PurchaseStates.waiting_for_card_receipt)

    card_lines = [
        "پرداخت کارت به کارت",
        "",
        f"پلن: {plan.name}",
        f"مبلغ: {toman_amount:,} تومان",
        f"شماره کارت: <code>{gw.card_number}</code>",
        f"نام صاحب کارت: {gw.card_holder}",
    ]
    if gw.card_bank:
        card_lines.append(f"بانک: {gw.card_bank}")
    if gw.card_note:
        card_lines.extend(["", gw.card_note])
    card_lines.extend(["", "بعد از پرداخت، عکس رسید را همینجا ارسال کنید."])
    await safe_edit_or_send(callback, "\n".join(card_lines))


@router.message(PurchaseStates.waiting_for_card_receipt)
async def purchase_card_receipt_submitted(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if message.from_user is None:
        return
    if message.text and message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("عملیات لغو شد.")
        return
    if not message.photo:
        await message.answer("لطفا عکس رسید پرداخت را ارسال کنید.")
        return

    data = await state.get_data()
    payment_id = data.get("card_payment_id")
    if not payment_id:
        await state.clear()
        await message.answer("اطلاعات پرداخت پیدا نشد. لطفا دوباره خرید را شروع کنید.")
        return

    payment = await session.get(Payment, UUID(payment_id))
    if payment is None:
        await state.clear()
        await message.answer("پرداخت پیدا نشد. لطفا دوباره تلاش کنید.")
        return

    receipt_file_id = message.photo[-1].file_id
    payload = dict(payment.callback_payload or {})
    payload["receipt_file_id"] = receipt_file_id
    payment.callback_payload = payload
    payment.payment_status = "pending_approval"
    payment.provider_payment_id = receipt_file_id
    await session.flush()
    await state.clear()

    await message.answer(
        "رسید شما ثبت شد و برای مدیر ارسال شد. بعد از تایید، کانفیگ به صورت خودکار ارسال می‌شود."
    )
    await _notify_admins_about_card_purchase(message, session, payment, receipt_file_id)


async def _notify_admins_about_card_purchase(
    message: Message,
    session: AsyncSession,
    payment: Payment,
    receipt_file_id: str,
) -> None:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    from core.config import settings as app_settings
    from models.user import User
    from sqlalchemy import select as sel

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    payload = payment.callback_payload or {}
    admin_ids: set[int] = set()
    if app_settings.owner_telegram_id:
        admin_ids.add(app_settings.owner_telegram_id)
    result = await session.execute(sel(User.telegram_id).where(User.role.in_(["admin", "owner"])))
    admin_ids.update(result.scalars().all())

    builder = InlineKeyboardBuilder()
    builder.button(text="تایید و تحویل کانفیگ", callback_data=f"mp:ok:{payment.id}")
    builder.button(text="رد پرداخت", callback_data=f"mp:no:{payment.id}")
    builder.adjust(1)

    caption = (
        "درخواست کارت به کارت\n\n"
        f"کاربر: {user.first_name if user else '-'}\n"
        f"Telegram ID: <code>{message.from_user.id}</code>\n"
        f"مبلغ: <b>{payment.pay_amount:,.0f} تومان</b>\n"
        f"پلن: {payload.get('plan_id')}\n"
        f"نام کانفیگ: {payload.get('config_name', '-')}\n\n"
        "بعد از بررسی رسید، تایید یا رد کنید."
    )

    for admin_id in admin_ids:
        try:
            await message.bot.send_photo(
                admin_id,
                photo=receipt_file_id,
                caption=caption,
                reply_markup=builder.as_markup(),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        except Exception as exc:
            logger.warning("Could not notify admin %s about card payment: %s", admin_id, exc)


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
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
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
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await state.clear()
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
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


async def _process_tronado_purchase(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return

    data = await state.get_data()
    plan_id = UUID(data["plan_id"])
    config_name = data["config_name"]
    discount_percent = int(data.get("discount_percent", 0))
    discount_id = data.get("discount_id")

    plan = await session.get(Plan, plan_id)
    if plan is None or not plan.is_active:
        await state.clear()
        await safe_edit_or_send(callback, Messages.PLAN_NOT_AVAILABLE)
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await state.clear()
        await safe_edit_or_send(callback, Messages.ACCOUNT_NOT_FOUND)
        return

    final_price = plan.price
    if discount_percent > 0:
        final_price = (final_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    from services.tronado.payments import create_tronado_invoice

    invoice = await create_tronado_invoice(
        session=session,
        user=user,
        amount_usd=final_price,
        kind="direct_purchase",
        description=f"Purchase plan {plan.name} for user {user.id}",
        callback_payload={
            "plan_id": str(plan.id),
            "config_name": config_name,
            "discount_percent": discount_percent,
            "discount_id": discount_id,
            "purpose": "direct_purchase",
            "source": "bot",
        },
    )
    await state.clear()
    await safe_edit_or_send(
        callback,
        (
            "فاکتور پرداخت ترونادو ساخته شد.\n\n"
            f"مبلغ: {final_price} USD\n"
            f"مقدار پرداخت: {invoice.tron_amount} TRX\n\n"
            "بعد از پرداخت و تایید، کانفیگ شما به صورت خودکار ساخته و ارسال می‌شود."
        ),
        reply_markup=build_topup_link_keyboard(invoice.invoice_url),
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
    try:
        await ensure_plan_available(session, plan.id)
    except PlanStockError:
        await state.clear()
        await safe_edit_or_send(callback, "موجودی این پلن تمام شده است.")
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
    
    # Use DB-configured API key if available, otherwise fall back to env
    gw = await AppSettingsRepository(session).get_gateway_settings()
    effective_tetra_key = gw.tetrapay_api_key if gw.tetrapay_api_key else settings.tetrapay_api_key.get_secret_value()

    try:
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=effective_tetra_key,
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
        callback, text, reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_web, bot_url=tx.payment_url_bot)
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

    # Build message with HTML
    import html
    esc = html.escape
    discount_line = ""
    if discount_percent > 0:
        discount_line = f"🏷 تخفیف: <b>{discount_percent}%</b> (قیمت اصلی: {esc(str(original_price))})\n"

    payment_label = "کیف پول" if payment_method == "wallet" else "درگاه پرداخت"

    text = (
        "✅ <b>کانفیگ شما آماده است!</b>\n\n"
        f"📛 نام: <b>{esc(config_name)}</b>\n"
        f"📦 پلن: <b>{esc(plan.name)}</b>\n"
        f"💾 حجم: <b>{esc(volume_label)}</b>\n"
        f"📅 مدت: <b>{plan.duration_days} روز</b>\n"
        f"💰 پرداخت شده: <b>{esc(str(final_price))} {esc(plan.currency)}</b>\n"
        f"💳 روش پرداخت: <b>{esc(payment_label)}</b>\n"
        f"{discount_line}"
        f"🕐 فعال‌سازی: <b>از اولین اتصال</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🔗 <b>ساب لینک (برای وارد کردن در اپ):</b>\n"
        f"<code>{esc(sub_link)}</code>\n\n"
        "📋 <b>کانفیگ مستقیم:</b>\n"
        f"<code>{esc(vless_uri)}</code>\n\n"
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
        is_active=True,
        bot_username=(bot._me.username if bot._me else (await bot.get_me()).username) if bot else None,
        vless_uri=vless_uri,
    )
    
    if banner_bytes:
        await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(banner_bytes.getvalue(), filename="banner.png"),
            caption=text,
            reply_markup=builder.as_markup() if vless_uri else None,
            parse_mode="HTML"
        )
    else:
        await bot.send_message(
            chat_id=chat_id, 
            text=text, 
            reply_markup=builder.as_markup() if vless_uri else None,
            parse_mode="HTML"
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
