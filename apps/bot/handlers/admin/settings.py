from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import GatewaySettingsStates, ReferralSettingsStates, SettingsStates
from core.texts import AdminButtons, AdminMessages
from repositories.settings import AppSettingsRepository
from apps.bot.utils.messaging import safe_edit_or_send

logger = logging.getLogger(__name__)

router = Router(name="admin-settings")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:bot_settings")
async def bot_settings_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    
    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()
    toman_rate = await settings_repo.get_toman_rate()
    
    text = AdminMessages.SETTINGS_MENU.format(
        price_per_gb=renewal_settings.price_per_gb,
        price_per_10_days=renewal_settings.price_per_10_days,
        toman_rate=f"{toman_rate:,}",
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر قیمت تمدید هر ۱ گیگ", callback_data="admin:settings:edit_gb")
    builder.button(text="تغییر قیمت تمدید هر ۱۰ روز", callback_data="admin:settings:edit_days")
    builder.button(text="💱 تغییر نرخ دلار به تومان", callback_data="admin:settings:edit_toman")
    builder.button(text="💳 مدیریت درگاه‌های پرداخت", callback_data="admin:settings:gateways")
    builder.button(text="🔗 تنظیمات رفرال", callback_data="admin:settings:referral")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:settings:edit_gb")
async def edit_price_gb_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_price_gb)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await safe_edit_or_send(callback,
        AdminMessages.ENTER_PRICE_PER_GB,
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_price_gb)
async def edit_price_gb_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        new_price = float(message.text.strip())
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_renewal_settings(price_per_gb=new_price)

    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:edit_days")
async def edit_price_days_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_price_days)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await safe_edit_or_send(callback,
        AdminMessages.ENTER_PRICE_PER_10_DAYS,
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_price_days)
async def edit_price_days_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        new_price = float(message.text.strip())
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_renewal_settings(price_per_10_days=new_price)

    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:edit_toman")
async def edit_toman_rate_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_toman_rate)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    
    await safe_edit_or_send(callback,
        "💱 نرخ فعلی دلار به تومان را وارد کنید.\n"
        "مثال: `85000`",
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_toman_rate)
async def edit_toman_rate_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        rate = int(message.text.strip().replace(",", ""))
        if rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد صحیح مثبت وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.set_toman_rate(rate)

    await state.clear()
    await message.answer(f"✅ نرخ دلار به تومان به {rate:,} تنظیم شد.")


# ─── Payment Gateway Management ──────────────────────────────────────────────


@router.callback_query(F.data == "admin:settings:gateways")
async def gateway_settings_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()

    nowpay_status = "🟢 فعال" if gw.nowpayments_enabled else "🔴 غیرفعال"
    tetra_status = "🟢 فعال" if gw.tetrapay_enabled else "🔴 غیرفعال"
    manual_status = "🟢 فعال" if gw.manual_crypto_enabled else "🔴 غیرفعال"

    # Mask API keys for display
    nowpay_key_display = _mask_api_key(gw.nowpayments_api_key) if gw.nowpayments_api_key else "پیش‌فرض (env)"
    tetra_key_display = _mask_api_key(gw.tetrapay_api_key) if gw.tetrapay_api_key else "پیش‌فرض (env)"
    ipn_secret_display = _mask_api_key(gw.nowpayments_ipn_secret) if gw.nowpayments_ipn_secret else "پیش‌فرض (env)"

    text = (
        "💳 مدیریت درگاه‌های پرداخت\n\n"
        f"💎 NOWPayments (ارزی): {nowpay_status}\n"
        f"   🔑 API Key: {nowpay_key_display}\n"
        f"   🔐 IPN Secret: {ipn_secret_display}\n\n"
        f"💳 تتراپی (ریالی): {tetra_status}\n"
        f"   🔑 API Key: {tetra_key_display}\n\n"
        f"💰 پرداخت دستی کریپتو: {manual_status}\n"
        f"   💱 ارز: {gw.manual_crypto_currency or 'تنظیم نشده'}\n"
        f"   📍 آدرس: {_mask_api_key(gw.manual_crypto_address) if gw.manual_crypto_address else 'تنظیم نشده'}\n"
    )

    builder = InlineKeyboardBuilder()
    toggle_nowpay_text = "🔴 غیرفعال کردن NOWPayments" if gw.nowpayments_enabled else "🟢 فعال کردن NOWPayments"
    toggle_tetra_text = "🔴 غیرفعال کردن تتراپی" if gw.tetrapay_enabled else "🟢 فعال کردن تتراپی"
    toggle_manual_text = "🔴 غیرفعال کردن پرداخت دستی" if gw.manual_crypto_enabled else "🟢 فعال کردن پرداخت دستی"

    builder.button(text=toggle_nowpay_text, callback_data="admin:gw:toggle_nowpay")
    builder.button(text=toggle_tetra_text, callback_data="admin:gw:toggle_tetra")
    builder.button(text="🔑 تغییر API Key نوپیمنتز", callback_data="admin:gw:edit_nowpay_key")
    builder.button(text="🔐 تغییر IPN Secret نوپیمنتز", callback_data="admin:gw:edit_ipn")
    builder.button(text="🔑 تغییر API Key تتراپی", callback_data="admin:gw:edit_tetra_key")
    builder.button(text=toggle_manual_text, callback_data="admin:gw:toggle_manual")
    builder.button(text="💱 تغییر ارز پرداخت دستی", callback_data="admin:gw:edit_manual_cur")
    builder.button(text="📍 تغییر آدرس ولت", callback_data="admin:gw:edit_manual_addr")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:toggle_nowpay")
async def toggle_nowpayments(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    await settings_repo.update_gateway_settings(nowpayments_enabled=not gw.nowpayments_enabled)
    await gateway_settings_menu(callback, session)


@router.callback_query(F.data == "admin:gw:toggle_tetra")
async def toggle_tetrapay(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    await settings_repo.update_gateway_settings(tetrapay_enabled=not gw.tetrapay_enabled)
    await gateway_settings_menu(callback, session)


@router.callback_query(F.data == "admin:gw:edit_nowpay_key")
async def edit_nowpay_key_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_nowpayments_api_key)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 حذف (استفاده از env)", callback_data="admin:gw:clear_nowpay_key")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "🔑 API Key جدید NOWPayments را وارد کنید:\n\n"
        "برای بازگشت به مقدار پیش‌فرض (env) دکمه حذف را بزنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:gw:clear_nowpay_key")
async def clear_nowpay_key(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(nowpayments_api_key=None)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_nowpayments_api_key)
async def edit_nowpay_key_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    api_key = message.text.strip()
    if len(api_key) < 5:
        await message.answer("❌ API Key خیلی کوتاه است. لطفاً مقدار معتبر وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(nowpayments_api_key=api_key)

    await state.clear()
    # Delete the message containing the API key for security
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ API Key نوپیمنتز به‌روزرسانی شد.\n🔑 {_mask_api_key(api_key)}")


@router.callback_query(F.data == "admin:gw:edit_tetra_key")
async def edit_tetra_key_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_tetrapay_api_key)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 حذف (استفاده از env)", callback_data="admin:gw:clear_tetra_key")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "🔑 API Key جدید تتراپی را وارد کنید:\n\n"
        "برای بازگشت به مقدار پیش‌فرض (env) دکمه حذف را بزنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:gw:clear_tetra_key")
async def clear_tetra_key(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(tetrapay_api_key=None)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_tetrapay_api_key)
async def edit_tetra_key_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    api_key = message.text.strip()
    if len(api_key) < 5:
        await message.answer("❌ API Key خیلی کوتاه است. لطفاً مقدار معتبر وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(tetrapay_api_key=api_key)

    await state.clear()
    # Delete the message containing the API key for security
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ API Key تتراپی به‌روزرسانی شد.\n🔑 {_mask_api_key(api_key)}")


# ─── NOWPayments IPN Secret ───────────────────────────────────────────────────


@router.callback_query(F.data == "admin:gw:edit_ipn")
async def edit_ipn_secret_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_nowpayments_ipn_secret)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 حذف (استفاده از env)", callback_data="admin:gw:clear_ipn")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "🔐 IPN Secret جدید NOWPayments را وارد کنید:\n\n"
        "این مقدار باید با IPN Secret در داشبورد NOWPayments یکی باشد.\n"
        "برای بازگشت به مقدار پیش‌فرض (env) دکمه حذف را بزنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:gw:clear_ipn")
async def clear_ipn_secret(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(nowpayments_ipn_secret=None)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_nowpayments_ipn_secret)
async def edit_ipn_secret_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    ipn_secret = message.text.strip()
    if len(ipn_secret) < 3:
        await message.answer("❌ IPN Secret خیلی کوتاه است. لطفاً مقدار معتبر وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(nowpayments_ipn_secret=ipn_secret)

    await state.clear()
    # Delete the message containing the secret for security
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ IPN Secret نوپیمنتز به‌روزرسانی شد.\n🔐 {_mask_api_key(ipn_secret)}")


# ─── Manual Crypto Wallet ─────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:gw:toggle_manual")
async def toggle_manual_crypto(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    await settings_repo.update_gateway_settings(manual_crypto_enabled=not gw.manual_crypto_enabled)
    await gateway_settings_menu(callback, session)


@router.callback_query(F.data == "admin:gw:edit_manual_cur")
async def edit_manual_currency_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_manual_currency)

    builder = InlineKeyboardBuilder()
    builder.button(text="💲 USDT TRC20", callback_data="admin:gw:quick_cur:USDT TRC20")
    builder.button(text="💲 USDT ERC20", callback_data="admin:gw:quick_cur:USDT ERC20")
    builder.button(text="₿ BTC", callback_data="admin:gw:quick_cur:BTC")
    builder.button(text="⟠ ETH", callback_data="admin:gw:quick_cur:ETH")
    builder.button(text="💎 TON", callback_data="admin:gw:quick_cur:TON")
    builder.button(text="Ł LTC", callback_data="admin:gw:quick_cur:LTC")
    builder.button(text="◈ TRX", callback_data="admin:gw:quick_cur:TRX")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(2, 2, 3, 1)

    await safe_edit_or_send(callback,
        "💱 نوع ارز پرداخت دستی را انتخاب کنید:\n\n"
        "یکی از ارزهای زیر را بزنید یا نام ارز دلخواه را تایپ کنید.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("admin:gw:quick_cur:"))
async def quick_set_currency(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    currency = callback.data.split(":", 3)[3]  # "admin:gw:quick_cur:USDT TRC20"
    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(manual_crypto_currency=currency)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_manual_currency)
async def edit_manual_currency_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    currency = message.text.strip().upper()
    if len(currency) < 2:
        await message.answer("❌ نام ارز خیلی کوتاه است.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(manual_crypto_currency=currency)
    await state.clear()
    await message.answer(f"✅ نوع ارز به «{currency}» تنظیم شد.")


@router.callback_query(F.data == "admin:gw:edit_manual_addr")
async def edit_manual_address_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_manual_address)

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "📍 آدرس ولت خود را وارد کنید:\n\n"
        "مشتری‌ها این آدرس را برای ارسال رمزارز می‌بینند.",
        reply_markup=builder.as_markup(),
    )


@router.message(GatewaySettingsStates.waiting_for_manual_address)
async def edit_manual_address_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    address = message.text.strip()
    if len(address) < 10:
        await message.answer("❌ آدرس ولت خیلی کوتاه است.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_gateway_settings(manual_crypto_address=address)
    await state.clear()
    # Delete message for security
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ آدرس ولت تنظیم شد.\n📍 {_mask_api_key(address)}")

# ─── Referral Settings ────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:settings:referral")
async def referral_settings_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    ref = await settings_repo.get_referral_settings()

    status = "🟢 فعال" if ref.enabled else "🔴 غیرفعال"

    text = (
        "🔗 تنظیمات سیستم رفرال\n\n"
        f"وضعیت: {status}\n"
        f"💰 پاداش معرف: {ref.referrer_bonus_usd:.2f} دلار\n"
        f"🎁 پاداش دعوت‌شده: {ref.referee_bonus_usd:.2f} دلار\n\n"
        "توضیح: وقتی کاربر دعوت‌شده اولین خرید خود را انجام دهد، "
        "معرف و (اگر تنظیم شده) دعوت‌شده پاداش دریافت می‌کنند."
    )

    builder = InlineKeyboardBuilder()
    toggle_text = "🔴 غیرفعال کردن رفرال" if ref.enabled else "🟢 فعال کردن رفرال"
    builder.button(text=toggle_text, callback_data="admin:ref:toggle")
    builder.button(text="💰 تغییر پاداش معرف", callback_data="admin:ref:edit_referrer")
    builder.button(text="🎁 تغییر پاداش دعوت‌شده", callback_data="admin:ref:edit_referee")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:ref:toggle")
async def toggle_referral(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    ref = await settings_repo.get_referral_settings()
    await settings_repo.update_referral_settings(enabled=not ref.enabled)
    await referral_settings_menu(callback, session)


@router.callback_query(F.data == "admin:ref:edit_referrer")
async def edit_referrer_bonus_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ReferralSettingsStates.waiting_for_referrer_bonus)

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:referral")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "💰 مبلغ پاداش معرف (به دلار) را وارد کنید.\nمثلاً: 0.5",
        reply_markup=builder.as_markup(),
    )


@router.message(ReferralSettingsStates.waiting_for_referrer_bonus)
async def edit_referrer_bonus_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        bonus = float(message.text.strip())
        if bonus < 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_referral_settings(referrer_bonus_usd=bonus)

    await state.clear()
    await message.answer(f"✅ پاداش معرف به {bonus:.2f} دلار تنظیم شد.")


@router.callback_query(F.data == "admin:ref:edit_referee")
async def edit_referee_bonus_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ReferralSettingsStates.waiting_for_referee_bonus)

    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:referral")
    builder.adjust(1)

    await safe_edit_or_send(callback,
        "🎁 مبلغ پاداش دعوت‌شده (به دلار) را وارد کنید.\n"
        "عدد ۰ یعنی بدون پاداش برای دعوت‌شده.\nمثلاً: 0.25",
        reply_markup=builder.as_markup(),
    )


@router.message(ReferralSettingsStates.waiting_for_referee_bonus)
async def edit_referee_bonus_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    try:
        bonus = float(message.text.strip())
        if bonus < 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر وارد کنید.")
        return

    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_referral_settings(referee_bonus_usd=bonus)

    await state.clear()
    await message.answer(f"✅ پاداش دعوت‌شده به {bonus:.2f} دلار تنظیم شد.")


# ─── Backup ───────────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:backup")
async def manual_backup_request(callback: CallbackQuery, session: AsyncSession) -> None:
    """Manually trigger the backup job and send to the requesting admin."""
    # We don't block the UI during the potentially slow backup process
    await callback.answer("⏳ در حال تهیه بکاپ، لطفا صبر کنید...", show_alert=True)
    if callback.from_user is None:
        return
        
    try:
        from apps.worker.jobs.backup import run_backup
        # Retrieve bot instance
        bot = callback.bot
        await run_backup(session, bot, manual_requester_id=callback.from_user.id)
    except Exception as exc:
        logger.error("Manual backup failed: %s", exc)
        try:
            await safe_edit_or_send(callback, "❌ خطا در اجرای بکاپ دستی.")
        except Exception:
            pass


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only first 4 and last 4 chars."""
    if len(key) <= 10:
        return key[:2] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]
