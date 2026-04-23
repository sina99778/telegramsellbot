from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import (
    build_topup_link_keyboard,
    build_wallet_profile_keyboard,
    build_wallet_topup_keyboard,
    build_wallet_history_keyboard,
)
from apps.bot.states.wallet import TopUpStates
from core.config import settings
from core.texts import Buttons, Messages
from models.payment import Payment
from repositories.user import UserRepository
from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="user-topup")


@router.message(F.text == Buttons.PROFILE_WALLET)
async def wallet_profile_handler(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None or user.wallet is None:
        await message.answer(Messages.WALLET_NOT_FOUND)
        return

    from repositories.settings import AppSettingsRepository
    from core.formatting import format_price_with_toman
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    balance_display = format_price_with_toman(user.wallet.balance, toman_rate)

    await message.answer(
        Messages.PROFILE_OVERVIEW.format(
            name=user.first_name or "کاربر",
            balance=balance_display,
            credit_limit=f"{user.wallet.credit_limit:.2f}",
        ),
        reply_markup=build_wallet_profile_keyboard(),
    )


@router.callback_query(F.data == "wallet:profile")
async def wallet_profile_callback_handler(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None or user.wallet is None:
        await safe_edit_or_send(callback, Messages.WALLET_NOT_FOUND)
        return

    from repositories.settings import AppSettingsRepository
    from core.formatting import format_price_with_toman
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    balance_display = format_price_with_toman(user.wallet.balance, toman_rate)

    await safe_edit_or_send(
        callback,
        Messages.PROFILE_OVERVIEW.format(
            name=user.first_name or "کاربر",
            balance=balance_display,
            credit_limit=f"{user.wallet.credit_limit:.2f}",
        ),
        reply_markup=build_wallet_profile_keyboard(),
    )


@router.callback_query(F.data == "wallet:history")
async def wallet_history_handler(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show last 10 wallet transactions."""
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None or user.wallet is None:
        await safe_edit_or_send(callback, Messages.WALLET_NOT_FOUND)
        return

    from sqlalchemy import select as sel
    from models.wallet import WalletTransaction

    result = await session.execute(
        sel(WalletTransaction)
        .where(WalletTransaction.wallet_id == user.wallet.id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(10)
    )
    transactions = list(result.scalars().all())

    if not transactions:
        await safe_edit_or_send(callback, "📭 هیچ تراکنشی ثبت نشده.")
        return

    type_labels = {
        "deposit": "➕ واریز",
        "purchase": "🛒 خرید",
        "renewal": "🔄 تمدید",
        "refund": "💰 بازپرداخت",
    }

    lines = ["📊 آخرین ۱۰ تراکنش کیف پول:\n"]
    for tx in transactions:
        direction_icon = "🟢" if tx.direction == "credit" else "🔴"
        label = type_labels.get(tx.type, tx.type)
        dt = tx.created_at.strftime("%Y-%m-%d %H:%M") if tx.created_at else ""
        lines.append(
            f"{direction_icon} {label}: {tx.amount:.2f} {tx.currency}\n"
            f"   موجودی: {tx.balance_after:.2f} | {dt}"
        )

    await safe_edit_or_send(callback, "\n\n".join(lines), reply_markup=build_wallet_history_keyboard())


@router.callback_query(F.data == "wallet:topup")
async def topup_options_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit_or_send(callback, 
        Messages.TOPUP_CHOOSE_AMOUNT,
        reply_markup=build_wallet_topup_keyboard(),
    )


@router.callback_query(F.data.startswith("wallet:topup:preset:"))
async def topup_preset_handler(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    raw_amount = callback.data.rsplit(":", 1)[-1]
    amount = Decimal(raw_amount)
    
    await state.update_data(topup_amount=str(amount))
    
    from apps.bot.keyboards.inline import build_gateway_selection_keyboard
    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    await safe_edit_or_send(
        callback,
        "💳 لطفاً درگاه پرداخت را انتخاب کنید:",
        reply_markup=build_gateway_selection_keyboard(
            nowpayments_enabled=gw.nowpayments_enabled,
            tetrapay_enabled=gw.tetrapay_enabled,
            manual_crypto_enabled=gw.manual_crypto_enabled and bool(gw.manual_crypto_address),
        )
    )


@router.callback_query(F.data == "wallet:topup:custom")
async def topup_custom_amount_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(TopUpStates.waiting_for_custom_amount)
    await safe_edit_or_send(callback, Messages.TOPUP_ENTER_CUSTOM)


@router.message(TopUpStates.waiting_for_custom_amount)
async def topup_custom_amount_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if message.from_user is None or message.text is None:
        return

    try:
        amount = Decimal(message.text.strip())
    except InvalidOperation:
        await message.answer(Messages.TOPUP_INVALID_AMOUNT)
        return

    if amount <= Decimal("0"):
        await message.answer(Messages.TOPUP_AMOUNT_GT_ZERO)
        return

    await state.update_data(topup_amount=str(amount))
    await state.set_state(None) # clear state
    
    from apps.bot.keyboards.inline import build_gateway_selection_keyboard
    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    await message.answer(
        "💳 لطفاً درگاه پرداخت را انتخاب کنید:",
        reply_markup=build_gateway_selection_keyboard(
            nowpayments_enabled=gw.nowpayments_enabled,
            tetrapay_enabled=gw.tetrapay_enabled,
            manual_crypto_enabled=gw.manual_crypto_enabled and bool(gw.manual_crypto_address),
        )
    )

@router.callback_query(F.data == "wallet:topup:pay:gateway")
async def topup_pay_gateway(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay with NOWPayments"""
    await callback.answer()

    # Check gateway enabled
    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.nowpayments_enabled:
        await safe_edit_or_send(callback, "❌ درگاه NOWPayments در حال حاضر غیرفعال است.")
        return

    data = await state.get_data()
    amount_str = data.get("topup_amount")
    if not amount_str:
        await safe_edit_or_send(callback, Messages.TOPUP_AMOUNT_GT_ZERO)
        return
        
    await _create_nowpayments_topup_invoice(
        callback.from_user.id, Decimal(amount_str), callback.message, session,
        api_key_override=gw.nowpayments_api_key,
    )


@router.callback_query(F.data == "wallet:topup:pay:tetrapay")
async def topup_pay_tetrapay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay with TetraPay"""
    await callback.answer()

    # Check gateway enabled
    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.tetrapay_enabled:
        await safe_edit_or_send(callback, "❌ درگاه تتراپی در حال حاضر غیرفعال است.")
        return

    data = await state.get_data()
    amount_str = data.get("topup_amount")
    if not amount_str:
        await safe_edit_or_send(callback, Messages.TOPUP_AMOUNT_GT_ZERO)
        return
        
    await _create_tetrapay_topup_invoice(
        callback.from_user.id, Decimal(amount_str), callback.message, session,
        api_key_override=gw.tetrapay_api_key,
    )




async def _create_nowpayments_topup_invoice(
    telegram_id: int,
    amount: Decimal,
    message: Message,
    session: AsyncSession,
    *,
    api_key_override: str | None = None,
) -> None:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    if user is None:
        await message.answer(Messages.ACCOUNT_NOT_FOUND)
        return

    local_order_id = str(uuid4())
    payload = NowPaymentsPaymentCreateRequest(
        price_amount=amount,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Wallet top-up for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )

    # Use DB-configured API key if available, otherwise fall back to env
    from pydantic import SecretStr
    effective_api_key = SecretStr(api_key_override) if api_key_override else settings.nowpayments_api_key

    try:
        async with NowPaymentsClient(
            NowPaymentsClientConfig(
                api_key=effective_api_key,
                base_url=settings.nowpayments_base_url,
            )
        ) as client:
            invoice = await client.create_payment_invoice(payload)
    except NowPaymentsRequestError:
        await message.answer(Messages.PAYMENT_GATEWAY_UNAVAILABLE)
        return

    payment = Payment(
        user_id=user.id,
        provider="nowpayments",
        kind="wallet_topup",
        provider_payment_id=None,
        provider_invoice_id=str(invoice.id),
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency=None,
        price_currency="USD",
        price_amount=amount,
        invoice_url=str(invoice.invoice_url),
        callback_payload={},
    )
    session.add(payment)
    await session.flush()

    await message.answer(
        Messages.TOPUP_INVOICE_CREATED.format(amount=amount),
        reply_markup=build_topup_link_keyboard(str(invoice.invoice_url)),
    )

async def _create_tetrapay_topup_invoice(
    telegram_id: int,
    amount: Decimal,
    message: Message,
    session: AsyncSession,
    *,
    api_key_override: str | None = None,
) -> None:
    user = await UserRepository(session).get_by_telegram_id(telegram_id)
    if user is None:
        await message.answer(Messages.ACCOUNT_NOT_FOUND)
        return

    from repositories.settings import AppSettingsRepository
    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        await message.answer("❌ نرخ تبدیل تومان تنظیم نشده. لطفاً با پشتیبانی تماس بگیرید.")
        return

    toman_amount = int((amount * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10

    # TetraPay minimum amount check (10,000 Rials = 1,000 Tomans)
    if rial_amount < 10000:
        await message.answer(
            f"❌ مبلغ پرداخت ({toman_amount:,} تومان) کمتر از حداقل مبلغ مجاز درگاه (۱,۰۰۰ تومان) است.\n"
            "لطفاً مبلغ بیشتری وارد کنید یا از درگاه ارزی استفاده کنید."
        )
        return

    # TetraPay maximum amount check
    max_toman = settings.tetrapay_max_amount_toman
    if toman_amount > max_toman:
        await message.answer(
            f"❌ مبلغ پرداخت ({toman_amount:,} تومان) بیشتر از سقف مجاز درگاه تتراپی ({max_toman:,} تومان) است.\n\n"
            "💡 لطفاً مبلغ کمتری وارد کنید یا از درگاه ارزی (NOWPayments) استفاده کنید."
        )
        return

    import logging
    logger = logging.getLogger(__name__)
    logger.info(
        "TetraPay topup: user=%s, amount_usd=%s, toman=%s, rial=%s",
        user.telegram_id, amount, toman_amount, rial_amount
    )

    local_order_id = str(uuid4())
    
    from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

    # Use DB-configured API key if available, otherwise fall back to env
    effective_tetra_key = api_key_override if api_key_override else settings.tetrapay_api_key.get_secret_value()
    
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
                description=f"شارژ کیف پول - کاربر {user.telegram_id}",
                email=f"{user.telegram_id}@telegram.org",
                mobile="09111111111",
            )
    except TetraPayRequestError as exc:
        logger.error("TetraPay topup create_order failed for user %s: %s", user.telegram_id, exc)
        await message.answer(f"❌ خطا در ساخت فاکتور تتراپی:\n{exc}\n\nلطفاً دوباره تلاش کنید.")
        return

    payment = Payment(
        user_id=user.id,
        provider="tetrapay",
        kind="wallet_topup",
        provider_payment_id=tx.Authority,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=amount,
        pay_amount=toman_amount,
        invoice_url=tx.payment_url_bot,
        callback_payload={},
    )
    session.add(payment)
    await session.flush()

    text = (
        "🔖 **فاکتور شارژ (ریالی/تومانی)**\n\n"
        f"💳 درگاه: تتراپی\n"
        f"💵 مبلغ پرداخت: `{toman_amount:,}` تومان\n"
        f"💰 شارژ دلاری: `{amount:.2f}` دلار\n\n"
        "👇 برای پرداخت روی دکمه زیر کلیک کنید:"
    )
    await message.answer(
        text,
        reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_web, bot_url=tx.payment_url_bot),
    )


# ─── Manual Crypto Payment ────────────────────────────────────────────────────


@router.callback_query(F.data == "wallet:topup:pay:manual")
async def topup_pay_manual(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show admin wallet address and ask user to send crypto, then submit hash."""
    await callback.answer()

    if callback.from_user is None:
        return

    from repositories.settings import AppSettingsRepository
    gw = await AppSettingsRepository(session).get_gateway_settings()

    if not gw.manual_crypto_enabled or not gw.manual_crypto_address:
        await safe_edit_or_send(callback, "❌ پرداخت دستی کریپتو غیرفعال یا آدرس تنظیم نشده.")
        return

    data = await state.get_data()
    amount_str = data.get("topup_amount")
    if not amount_str:
        await safe_edit_or_send(callback, Messages.TOPUP_AMOUNT_GT_ZERO)
        return

    amount = Decimal(amount_str)
    currency = gw.manual_crypto_currency or "Crypto"
    address = gw.manual_crypto_address

    # Convert USD to crypto amount in real-time
    from services.crypto_price import convert_usd_to_crypto
    crypto_amount, unit_price = await convert_usd_to_crypto(amount, currency)

    # Currency emoji map
    _cur_emoji = {
        "BTC": "₿", "ETH": "⟠", "TON": "💎", "LTC": "Ł",
        "TRX": "◈", "USDT TRC20": "💲", "USDT ERC20": "💲", "USDT": "💲",
    }
    cur_icon = _cur_emoji.get(currency, "🪙")

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {cur_icon} پرداخت {currency}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 مبلغ:  <b>{amount:.2f} USD</b>\n"
    )

    if crypto_amount is not None and unit_price is not None:
        # Format based on currency precision
        if currency in {"BTC"}:
            crypto_display = f"{crypto_amount:.8f}"
        elif currency in {"ETH", "LTC", "TON"}:
            crypto_display = f"{crypto_amount:.6f}"
        else:
            crypto_display = f"{crypto_amount:.4f}"

        text += (
            f"{cur_icon} معادل:  <b>{crypto_display} {currency}</b>\n"
            f"📊 نرخ لحظه‌ای:  1 {currency} = {unit_price:,.2f} USD\n"
        )
    else:
        text += f"⚠️ نرخ لحظه‌ای در دسترس نیست.\n"

    text += (
        f"\n"
        f"┌─────────────────────\n"
        f"│ 📍 آدرس ولت:\n"
        f"│\n"
        f"│ <code>{address}</code>\n"
        f"└─────────────────────\n"
        f"\n"
        f"⚠️ لطفاً <b>دقیقاً</b> به همین آدرس واریز کنید.\n"
        f"\n"
        f"📝 پس از پرداخت، <b>هش تراکنش (TX Hash)</b>\n"
        f"    خود را در همینجا ارسال کنید.\n"
        f"\n"
        f"💡 برای لغو /cancel بزنید."
    )

    # Create a pending payment record
    local_order_id = str(uuid4())
    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "حساب پیدا نشد.")
        return

    payment = Payment(
        user_id=user.id,
        provider="manual_crypto",
        kind="wallet_topup",
        order_id=local_order_id,
        payment_status="waiting_hash",
        pay_currency=currency,
        price_currency="USD",
        price_amount=amount,
        callback_payload={
            "manual": True,
            "currency": currency,
            "crypto_amount": str(crypto_amount) if crypto_amount else None,
            "unit_price": str(unit_price) if unit_price else None,
        },
    )
    session.add(payment)
    await session.flush()

    await state.update_data(manual_payment_id=str(payment.id))
    await state.set_state(TopUpStates.waiting_for_manual_hash)

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ لغو عملیات", callback_data="wallet:topup")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.message(TopUpStates.waiting_for_manual_hash)
async def manual_hash_submitted(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User submits the transaction hash after sending crypto."""
    if not message.text or message.from_user is None:
        return

    if message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.")
        return

    tx_hash = message.text.strip()
    if len(tx_hash) < 10:
        await message.answer("❌ هش تراکنش خیلی کوتاه است. لطفاً هش کامل را ارسال کنید.")
        return

    data = await state.get_data()
    await state.clear()

    payment_id = data.get("manual_payment_id")
    if not payment_id:
        await message.answer("❌ اطلاعات پرداخت یافت نشد. لطفاً دوباره تلاش کنید.")
        return

    from uuid import UUID
    payment = await session.get(Payment, UUID(payment_id))
    if payment is None:
        await message.answer("❌ رکورد پرداخت یافت نشد.")
        return

    # Update payment with hash
    payment.payment_status = "pending_approval"
    payment.provider_payment_id = tx_hash
    if isinstance(payment.callback_payload, dict):
        payment.callback_payload = {**payment.callback_payload, "tx_hash": tx_hash}
    else:
        payment.callback_payload = {"tx_hash": tx_hash}
    await session.flush()

    await message.answer(
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "  ✅ هش تراکنش ثبت شد\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 Hash:\n<code>{tx_hash}</code>\n\n"
        f"💵 مبلغ: <b>{payment.price_amount:.2f} USD</b>\n"
        f"💱 ارز: {payment.pay_currency}\n\n"
        "⏳ پرداخت شما در صف بررسی قرار گرفت.\n"
        "پس از تأیید مدیر، مبلغ به کیف پول\n"
        "شما واریز خواهد شد.\n\n"
        "🔔 نتیجه از طریق ربات اطلاع‌رسانی می‌شود."
    )

    # Notify admins with approve/reject buttons
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    user_display = f"{user.first_name or '-'}" if user else "-"
    user_tg = message.from_user.id

    # Get crypto amount from payload if available
    payload = payment.callback_payload or {}
    crypto_amt = payload.get("crypto_amount")
    crypto_rate = payload.get("unit_price")
    crypto_info = ""
    if crypto_amt and crypto_rate:
        crypto_info = (
            f"🪙 معادل ارزی: <b>{crypto_amt} {payment.pay_currency}</b>\n"
            f"📊 نرخ: 1 {payment.pay_currency} = {crypto_rate} USD\n"
        )

    admin_text = (
        "🔔━━━━━━━━━━━━━━━━━━━━━🔔\n"
        "  💰 درخواست پرداخت دستی\n"
        "🔔━━━━━━━━━━━━━━━━━━━━━🔔\n\n"
        f"👤 کاربر: <b>{user_display}</b>\n"
        f"🆔 Telegram ID: <code>{user_tg}</code>\n\n"
        f"💵 مبلغ: <b>{payment.price_amount:.2f} USD</b>\n"
        f"💱 ارز: {payment.pay_currency}\n"
        f"{crypto_info}\n"
        f"🔗 TX Hash:\n<code>{tx_hash}</code>\n\n"
        "👇 لطفاً پس از بررسی تراکنش تأیید یا رد کنید."
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید و واریز به کیف پول", callback_data=f"admin:manual_pay:approve:{payment.id}")
    builder.button(text="❌ رد پرداخت", callback_data=f"admin:manual_pay:reject:{payment.id}")
    builder.adjust(1)

    # Send to all admins with buttons
    from core.config import settings as app_settings
    from sqlalchemy import select
    from models.user import User
    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
    import logging

    _logger = logging.getLogger(__name__)
    admin_ids: set[int] = set()
    if app_settings.owner_telegram_id:
        admin_ids.add(app_settings.owner_telegram_id)
    try:
        result = await session.execute(
            select(User.telegram_id).where(User.role.in_(["admin", "owner"]))
        )
        for row in result.scalars().all():
            admin_ids.add(row)
    except Exception:
        pass

    bot = message.bot
    for admin_tg_id in admin_ids:
        try:
            await bot.send_message(
                admin_tg_id,
                admin_text,
                reply_markup=builder.as_markup(),
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception as exc:
            _logger.warning("Could not notify admin %s: %s", admin_tg_id, exc)
