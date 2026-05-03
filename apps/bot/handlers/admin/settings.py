from __future__ import annotations

import logging
import json

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
from apps.bot.utils.panels import admin_panel, status_label
from services.telegram.premium_emoji import clear_premium_emoji_cache, parse_emoji_map_text

logger = logging.getLogger(__name__)

router = Router(name="admin-settings")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:bot_settings")
async def bot_settings_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    
    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()
    custom_settings = await settings_repo.get_custom_purchase_settings()
    security_settings = await settings_repo.get_service_security_settings()
    premium_emoji_settings = await settings_repo.get_premium_emoji_settings()
    toman_rate = await settings_repo.get_toman_rate()
    
    text = admin_panel(
        "⚙️ تنظیمات عمومی ربات",
        [
            (
                "قیمت‌ها",
                [
                    ("تمدید هر ۱ گیگ", f"{renewal_settings.price_per_gb} دلار"),
                    ("تمدید هر ۱۰ روز", f"{renewal_settings.price_per_10_days} دلار"),
                    ("نرخ دلار", f"{toman_rate:,} تومان"),
                ],
            ),
            (
                "خرید دلخواه",
                [
                    ("وضعیت", status_label(custom_settings.enabled)),
                    ("هر ۱ گیگ", f"{custom_settings.price_per_gb} دلار"),
                    ("هر ۱ روز", f"{custom_settings.price_per_day} دلار"),
                ],
            ),
            (
                "امنیت سرویس",
                [
                    ("limitIp پنل", security_settings.xui_limit_ip),
                    ("سقف IP مجاز", security_settings.max_distinct_ips),
                    ("ضد اشتراک‌گذاری", status_label(security_settings.auto_disable_ip_abuse)),
                ],
            ),
            (
                "نمایش",
                [
                    ("اموجی پریمیم", status_label(premium_emoji_settings.enabled)),
                    ("اموجی‌های تنظیم‌شده", len(premium_emoji_settings.emoji_map)),
                ],
            ),
        ],
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="قیمت گیگ", callback_data="admin:settings:edit_gb")
    builder.button(text="قیمت روز", callback_data="admin:settings:edit_days")
    builder.button(text="خرید دلخواه", callback_data="admin:settings:custom_toggle")
    builder.button(text="قیمت گیگ دلخواه", callback_data="admin:settings:custom_gb")
    builder.button(text="قیمت روز دلخواه", callback_data="admin:settings:custom_day")
    builder.button(text="limitIp", callback_data="admin:settings:xui_limit_ip")
    builder.button(text="سقف IP", callback_data="admin:settings:max_ips")
    builder.button(text="ضد اشتراک‌گذاری", callback_data="admin:settings:ip_guard_toggle")
    builder.button(text="اموجی پریمیم", callback_data="admin:settings:premium_emoji_toggle")
    builder.button(text="مپ اموجی", callback_data="admin:settings:premium_emoji_map")
    builder.button(text="نرخ دلار", callback_data="admin:settings:edit_toman")
    builder.button(text="درگاه‌ها", callback_data="admin:settings:gateways")
    builder.button(text="تایید موبایل", callback_data="admin:settings:phone_verification")
    builder.button(text="کانفیگ تست", callback_data="admin:settings:trial_toggle")
    builder.button(text="رفرال", callback_data="admin:settings:referral")
    builder.button(text="جوین اجباری", callback_data="admin:settings:force_join")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(2, 1, 2, 3, 2, 3, 1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


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


@router.callback_query(F.data == "admin:settings:custom_toggle")
async def toggle_custom_purchase(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    custom = await settings_repo.get_custom_purchase_settings()
    await settings_repo.update_custom_purchase_settings(enabled=not custom.enabled)
    await bot_settings_menu(callback, session)


@router.callback_query(F.data == "admin:settings:custom_gb")
async def edit_custom_price_gb_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_custom_price_gb)
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    await safe_edit_or_send(
        callback,
        "قیمت خرید دلخواه برای هر ۱ گیگابایت (به دلار) را وارد کنید:",
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_custom_price_gb)
async def edit_custom_price_gb_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        new_price = float(message.text.strip().replace(",", "."))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return
    await AppSettingsRepository(session).update_custom_purchase_settings(price_per_gb=new_price)
    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:custom_day")
async def edit_custom_price_day_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_custom_price_day)
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    await safe_edit_or_send(
        callback,
        "قیمت خرید دلخواه برای هر ۱ روز (به دلار) را وارد کنید:",
        reply_markup=builder.as_markup(),
    )


@router.message(SettingsStates.waiting_for_custom_price_day)
async def edit_custom_price_day_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        new_price = float(message.text.strip().replace(",", "."))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer(AdminMessages.INVALID_PRICE)
        return
    await AppSettingsRepository(session).update_custom_purchase_settings(price_per_day=new_price)
    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:xui_limit_ip")
async def edit_xui_limit_ip_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_xui_limit_ip)
    await safe_edit_or_send(callback, "عدد limitIp پنل را وارد کنید. 0 یعنی بدون محدودیت، 1 یعنی فقط یک IP همزمان.")


@router.message(SettingsStates.waiting_for_xui_limit_ip)
async def edit_xui_limit_ip_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        value = int(message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد معتبر وارد کنید.")
        return

    await AppSettingsRepository(session).update_service_security_settings(xui_limit_ip=value)
    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:max_ips")
async def edit_max_ips_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_max_distinct_ips)
    await safe_edit_or_send(callback, "سقف IPهای distinct مجاز برای هر کانفیگ را وارد کنید. پیشنهاد: 3. عدد 0 یعنی پایش خودکار خاموش شود.")


@router.message(SettingsStates.waiting_for_max_distinct_ips)
async def edit_max_ips_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        value = int(message.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد معتبر وارد کنید.")
        return

    await AppSettingsRepository(session).update_service_security_settings(max_distinct_ips=value)
    await state.clear()
    await message.answer(AdminMessages.SETTINGS_UPDATED)


@router.callback_query(F.data == "admin:settings:ip_guard_toggle")
async def toggle_ip_guard(callback: CallbackQuery, session: AsyncSession) -> None:
    settings_repo = AppSettingsRepository(session)
    current = await settings_repo.get_service_security_settings()
    await settings_repo.update_service_security_settings(
        auto_disable_ip_abuse=not current.auto_disable_ip_abuse,
    )
    await bot_settings_menu(callback, session)


@router.callback_query(F.data == "admin:settings:premium_emoji_toggle")
async def toggle_premium_emoji(callback: CallbackQuery, session: AsyncSession) -> None:
    settings_repo = AppSettingsRepository(session)
    current = await settings_repo.get_premium_emoji_settings()
    await settings_repo.update_premium_emoji_settings(enabled=not current.enabled)
    clear_premium_emoji_cache()
    await bot_settings_menu(callback, session)


@router.callback_query(F.data == "admin:settings:premium_emoji_map")
async def edit_premium_emoji_map_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_premium_emoji_map)
    
    settings_repo = AppSettingsRepository(session)
    current_settings = await settings_repo.get_premium_emoji_settings()
    emoji_map = current_settings.emoji_map or {}
    
    from services.telegram.premium_emoji import DEFAULT_EMOJI_KEYS
    
    lines = []
    for key, fallback in DEFAULT_EMOJI_KEYS.items():
        custom_id = emoji_map.get(key)
        if custom_id:
            # Render actual custom emoji if possible
            line = f"<code>{key}=</code><tg-emoji emoji-id=\"{custom_id}\">{fallback}</tg-emoji>"
        else:
            line = f"<code>{key}=</code>{fallback}"
        lines.append(line)
        
    template = "\n".join(lines)
    
    msg = (
        "✨ <b>مدیریت آسان اموجی‌های پرمیوم</b>\n\n"
        "برای تغییر اموجی‌ها، کافیست <b>لیست زیر را کپی کنید</b>، "
        "اموجی پیش‌فرض را پاک کرده و <b>اموجی پرمیوم خود را جایگزین کنید</b> (یا آیدی آن را بگذارید) و بفرستید:\n\n"
        f"{template}\n\n"
        "<i>برای لغو /cancel را بفرستید.</i>"
    )
    
    await safe_edit_or_send(
        callback,
        msg,
        parse_mode="HTML"
    )


@router.message(SettingsStates.waiting_for_premium_emoji_map)
async def edit_premium_emoji_map_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    html_text = message.html_text or ""
    
    emoji_map = {}
    import re
    from services.telegram.premium_emoji import VALID_CUSTOM_EMOJI_ID_RE, clear_premium_emoji_cache
    
    for line in html_text.splitlines():
        clean_line = line.strip()
        if not clean_line or "=" not in clean_line:
            continue
            
        key_part, val_part = clean_line.split("=", 1)
        clean_key = re.sub(r'<[^>]+>', '', key_part).strip()
        
        if not clean_key:
            continue
            
        match = re.search(r'emoji-id=["\']([^"\']+)["\']', val_part)
        if match:
            custom_id = match.group(1)
            emoji_map[clean_key] = custom_id
        else:
            clean_val = re.sub(r'<[^>]+>', '', val_part).strip()
            if VALID_CUSTOM_EMOJI_ID_RE.match(clean_val):
                emoji_map[clean_key] = clean_val

    if not emoji_map:
        await message.answer(
            "هیچ اموجی پرمیوم یا ID معتبری در پیام شما یافت نشد.\n"
            "لطفاً مطمئن شوید که قالب (کلید=اموجی) را رعایت کرده‌اید."
        )
        return

    # Update settings
    settings_repo = AppSettingsRepository(session)
    current_settings = await settings_repo.get_premium_emoji_settings()
    
    # Merge with existing map so we don't delete keys that weren't sent
    new_map = dict(current_settings.emoji_map or {})
    new_map.update(emoji_map)

    await settings_repo.update_premium_emoji_settings(
        enabled=True,
        emoji_map=new_map,
    )
    clear_premium_emoji_cache()
    await state.clear()
    
    await message.answer(
        f"✅ <b>تغییرات ذخیره شد!</b>\n\nتعداد اموجی‌های دریافت‌شده: {len(emoji_map)}\nتعداد کل اموجی‌های فعال: {len(new_map)}",
        parse_mode="HTML"
    )


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
    tronado_status = "🟢 فعال" if gw.tronado_enabled else "🔴 غیرفعال"
    manual_status = "🟢 فعال" if gw.manual_crypto_enabled else "🔴 غیرفعال"
    card_status = "🟢 فعال" if gw.card_to_card_enabled else "🔴 غیرفعال"

    # Mask API keys for display
    nowpay_key_display = _mask_api_key(gw.nowpayments_api_key) if gw.nowpayments_api_key else "پیش‌فرض (env)"
    tetra_key_display = _mask_api_key(gw.tetrapay_api_key) if gw.tetrapay_api_key else "پیش‌فرض (env)"
    tronado_key_display = _mask_api_key(gw.tronado_api_key) if gw.tronado_api_key else "پیش‌فرض (env)"
    tronado_wallet_display = _mask_api_key(gw.tronado_wallet_address) if gw.tronado_wallet_address else "پیش‌فرض (env)"
    ipn_secret_display = _mask_api_key(gw.nowpayments_ipn_secret) if gw.nowpayments_ipn_secret else "پیش‌فرض (env)"
    manual_wallets = gw.manual_crypto_wallets or []
    wallets_display = "\n".join(
        f"   {index}. {wallet.get('currency', 'Crypto')}: {_mask_api_key(wallet.get('address', ''))}"
        for index, wallet in enumerate(manual_wallets, start=1)
    ) or "   تنظیم نشده"

    text = (
        "💳 مدیریت درگاه‌های پرداخت\n\n"
        f"💎 NOWPayments (ارزی): {nowpay_status}\n"
        f"   🔑 API Key: {nowpay_key_display}\n"
        f"   🔐 IPN Secret: {ipn_secret_display}\n\n"
        f"💳 تتراپی (ریالی): {tetra_status}\n"
        f"   🔑 API Key: {tetra_key_display}\n\n"
        f"ترونادو: {tronado_status}\n"
        f"   🔑 API Key: {tronado_key_display}\n"
        f"   ولت: {tronado_wallet_display}\n\n"
        f"💰 پرداخت دستی کریپتو: {manual_status}\n"
        f"   💱 ارز: {gw.manual_crypto_currency or 'تنظیم نشده'}\n"
        f"   📍 آدرس: {_mask_api_key(gw.manual_crypto_address) if gw.manual_crypto_address else 'تنظیم نشده'}\n"
        f"   👛 ولت‌ها:\n{wallets_display}\n"
        f"\n💳 کارت به کارت: {card_status}\n"
        f"   شماره کارت: {_mask_api_key(gw.card_number) if gw.card_number else 'تنظیم نشده'}\n"
        f"   صاحب کارت: {gw.card_holder or 'تنظیم نشده'}\n"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="NOWPayments", callback_data="admin:gw:nowpayments")
    builder.button(text="TetraPay", callback_data="admin:gw:tetrapay")
    builder.button(text="Tronado", callback_data="admin:gw:tronado")
    builder.button(text="پرداخت دستی کریپتو", callback_data="admin:gw:manual_menu")
    builder.button(text="کارت به کارت", callback_data="admin:gw:card")
    toggle_nowpay_text = "🔴 غیرفعال کردن NOWPayments" if gw.nowpayments_enabled else "🟢 فعال کردن NOWPayments"
    toggle_tetra_text = "🔴 غیرفعال کردن تتراپی" if gw.tetrapay_enabled else "🟢 فعال کردن تتراپی"
    toggle_tronado_text = "🔴 غیرفعال کردن ترونادو" if gw.tronado_enabled else "🟢 فعال کردن ترونادو"
    toggle_manual_text = "🔴 غیرفعال کردن پرداخت دستی" if gw.manual_crypto_enabled else "🟢 فعال کردن پرداخت دستی"

    builder.button(text=toggle_nowpay_text, callback_data="admin:gw:toggle_nowpay")
    builder.button(text=toggle_tetra_text, callback_data="admin:gw:toggle_tetra")
    builder.button(text=toggle_tronado_text, callback_data="admin:gw:toggle_tronado")
    builder.button(text="🔑 تغییر API Key نوپیمنتز", callback_data="admin:gw:edit_nowpay_key")
    builder.button(text="🔐 تغییر IPN Secret نوپیمنتز", callback_data="admin:gw:edit_ipn")
    builder.button(text="🔑 تغییر API Key تتراپی", callback_data="admin:gw:edit_tetra_key")
    builder.button(text="🔑 تغییر API Key ترونادو", callback_data="admin:gw:edit_tronado_key")
    builder.button(text="تغییر ولت ترونادو", callback_data="admin:gw:edit_tronado_wallet")
    builder.button(text=toggle_manual_text, callback_data="admin:gw:toggle_manual")
    builder.button(text="💱 تغییر ارز پرداخت دستی", callback_data="admin:gw:edit_manual_cur")
    builder.button(text="📍 تغییر آدرس ولت", callback_data="admin:gw:edit_manual_addr")
    builder.button(text="🗑 حذف ولت‌های دستی", callback_data="admin:gw:clear_manual_wallets")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:settings:trial_toggle")
async def toggle_trial_config(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    trial = await repo.get_trial_settings()
    updated = await repo.update_trial_settings(enabled=not trial.enabled)
    status = "فعال" if updated.enabled else "غیرفعال"
    await safe_edit_or_send(callback, f"کانفیگ تست {status} شد.")


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


@router.callback_query(F.data == "admin:gw:toggle_tronado")
async def toggle_tronado(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    await settings_repo.update_gateway_settings(tronado_enabled=not gw.tronado_enabled)
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


@router.callback_query(F.data == "admin:gw:edit_tronado_key")
async def edit_tronado_key_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_tronado_api_key)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 حذف (استفاده از env)", callback_data="admin:gw:clear_tronado_key")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)

    await safe_edit_or_send(
        callback,
        "API Key جدید ترونادو را وارد کنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:gw:clear_tronado_key")
async def clear_tronado_key(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    await AppSettingsRepository(session).update_gateway_settings(tronado_api_key=None)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_tronado_api_key)
async def edit_tronado_key_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    api_key = message.text.strip()
    if len(api_key) < 5:
        await message.answer("API Key خیلی کوتاه است.")
        return
    await AppSettingsRepository(session).update_gateway_settings(tronado_api_key=api_key)
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"API Key ترونادو به‌روزرسانی شد: {_mask_api_key(api_key)}")


@router.callback_query(F.data == "admin:gw:edit_tronado_wallet")
async def edit_tronado_wallet_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_tronado_wallet_address)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 حذف (استفاده از env)", callback_data="admin:gw:clear_tronado_wallet")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, "آدرس ولت TRON مقصد ترونادو را وارد کنید:", reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:clear_tronado_wallet")
async def clear_tronado_wallet(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await state.clear()
    await AppSettingsRepository(session).update_gateway_settings(tronado_wallet_address=None)
    await gateway_settings_menu(callback, session)


@router.message(GatewaySettingsStates.waiting_for_tronado_wallet_address)
async def edit_tronado_wallet_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    wallet = message.text.strip()
    if len(wallet) < 20:
        await message.answer("آدرس ولت معتبر نیست.")
        return
    await AppSettingsRepository(session).update_gateway_settings(tronado_wallet_address=wallet)
    await state.clear()
    await message.answer(f"ولت ترونادو به‌روزرسانی شد: {_mask_api_key(wallet)}")


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
    builder.button(text="💲 USDT BSC", callback_data="admin:gw:quick_cur:USDT BSC")
    builder.button(text="₿ BTC", callback_data="admin:gw:quick_cur:BTC")
    builder.button(text="⟠ ETH", callback_data="admin:gw:quick_cur:ETH")
    builder.button(text="💎 TON", callback_data="admin:gw:quick_cur:TON")
    builder.button(text="Ł LTC", callback_data="admin:gw:quick_cur:LTC")
    builder.button(text="◈ TRX", callback_data="admin:gw:quick_cur:TRX")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(2, 2, 2, 2, 1)

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
    gw = await settings_repo.get_gateway_settings()
    currency = gw.manual_crypto_currency or "Crypto"
    wallets = [
        wallet for wallet in gw.manual_crypto_wallets
        if wallet.get("address") != address or wallet.get("currency") != currency
    ]
    wallets.append({"currency": currency, "address": address})
    await settings_repo.update_gateway_settings(
        manual_crypto_address=address,
        manual_crypto_wallets=wallets,
    )
    await state.clear()
    # Delete message for security
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ آدرس ولت تنظیم شد.\n📍 {_mask_api_key(address)}")

# ─── Referral Settings ────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:gw:clear_manual_wallets")
async def clear_manual_wallets(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await AppSettingsRepository(session).update_gateway_settings(
        manual_crypto_address=None,
        manual_crypto_wallets=[],
    )
    await gateway_settings_menu(callback, session)


@router.callback_query(F.data == "admin:gw:nowpayments")
async def nowpayments_gateway_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    status = "فعال" if gw.nowpayments_enabled else "غیرفعال"
    text = (
        "NOWPayments\n\n"
        f"وضعیت: {status}\n"
        f"API Key: {_mask_api_key(gw.nowpayments_api_key) if gw.nowpayments_api_key else 'env'}\n"
        f"IPN Secret: {_mask_api_key(gw.nowpayments_ipn_secret) if gw.nowpayments_ipn_secret else 'env'}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر وضعیت", callback_data="admin:gw:toggle_nowpay")
    builder.button(text="API Key", callback_data="admin:gw:edit_nowpay_key")
    builder.button(text="IPN Secret", callback_data="admin:gw:edit_ipn")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:tetrapay")
async def tetrapay_gateway_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    status = "فعال" if gw.tetrapay_enabled else "غیرفعال"
    text = (
        "TetraPay\n\n"
        f"وضعیت: {status}\n"
        f"API Key: {_mask_api_key(gw.tetrapay_api_key) if gw.tetrapay_api_key else 'env'}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر وضعیت", callback_data="admin:gw:toggle_tetra")
    builder.button(text="API Key", callback_data="admin:gw:edit_tetra_key")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:tronado")
async def tronado_gateway_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    status = "فعال" if gw.tronado_enabled else "غیرفعال"
    text = (
        "Tronado\n\n"
        f"وضعیت: {status}\n"
        f"API Key: {_mask_api_key(gw.tronado_api_key) if gw.tronado_api_key else 'env'}\n"
        f"Wallet: {_mask_api_key(gw.tronado_wallet_address) if gw.tronado_wallet_address else 'env'}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر وضعیت", callback_data="admin:gw:toggle_tronado")
    builder.button(text="API Key", callback_data="admin:gw:edit_tronado_key")
    builder.button(text="Wallet", callback_data="admin:gw:edit_tronado_wallet")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:manual_menu")
async def manual_crypto_gateway_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    status = "فعال" if gw.manual_crypto_enabled else "غیرفعال"
    wallets = "\n".join(
        f"{i}. {w.get('currency', 'Crypto')}: {_mask_api_key(w.get('address', ''))}"
        for i, w in enumerate(gw.manual_crypto_wallets or [], start=1)
    ) or "تنظیم نشده"
    text = (
        "پرداخت دستی کریپتو\n\n"
        f"وضعیت: {status}\n"
        f"ارز پیش‌فرض: {gw.manual_crypto_currency or 'تنظیم نشده'}\n"
        f"ولت‌ها:\n{wallets}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="تغییر وضعیت", callback_data="admin:gw:toggle_manual")
    builder.button(text="ارز", callback_data="admin:gw:edit_manual_cur")
    builder.button(text="افزودن/تغییر آدرس ولت", callback_data="admin:gw:edit_manual_addr")
    builder.button(text="حذف ولت‌ها", callback_data="admin:gw:clear_manual_wallets")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:card")
async def card_to_card_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    status = "فعال" if gw.card_to_card_enabled else "غیرفعال"
    text = (
        "مدیریت کارت به کارت\n\n"
        f"وضعیت: {status}\n"
        f"شماره کارت: <code>{gw.card_number or 'تنظیم نشده'}</code>\n"
        f"صاحب کارت: {gw.card_holder or 'تنظیم نشده'}\n"
        f"بانک: {gw.card_bank or 'تنظیم نشده'}\n"
        f"توضیح: {gw.card_note or 'تنظیم نشده'}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="غیرفعال کردن" if gw.card_to_card_enabled else "فعال کردن",
        callback_data="admin:gw:card_toggle",
    )
    builder.button(text="شماره کارت", callback_data="admin:gw:card_number")
    builder.button(text="نام صاحب کارت", callback_data="admin:gw:card_holder")
    builder.button(text="نام بانک", callback_data="admin:gw:card_bank")
    builder.button(text="توضیح پرداخت", callback_data="admin:gw:card_note")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:gw:card_toggle")
async def card_to_card_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    gw = await repo.get_gateway_settings()
    await repo.update_gateway_settings(card_to_card_enabled=not gw.card_to_card_enabled)
    await card_to_card_menu(callback, session)


@router.callback_query(F.data == "admin:gw:card_number")
async def card_number_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_card_number)
    await safe_edit_or_send(callback, "شماره کارت را وارد کنید:", reply_markup=_gateway_back_keyboard("admin:gw:card"))


@router.message(GatewaySettingsStates.waiting_for_card_number)
async def card_number_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    card_number = message.text.strip().replace(" ", "").replace("-", "")
    if not card_number.isdigit() or len(card_number) < 12:
        await message.answer("شماره کارت معتبر نیست.")
        return
    await AppSettingsRepository(session).update_gateway_settings(card_number=card_number)
    await state.clear()
    await message.answer("شماره کارت ذخیره شد.")


@router.callback_query(F.data == "admin:gw:card_holder")
async def card_holder_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_card_holder)
    await safe_edit_or_send(callback, "نام صاحب کارت را وارد کنید:", reply_markup=_gateway_back_keyboard("admin:gw:card"))


@router.message(GatewaySettingsStates.waiting_for_card_holder)
async def card_holder_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    await AppSettingsRepository(session).update_gateway_settings(card_holder=message.text.strip())
    await state.clear()
    await message.answer("نام صاحب کارت ذخیره شد.")


@router.callback_query(F.data == "admin:gw:card_bank")
async def card_bank_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_card_bank)
    await safe_edit_or_send(callback, "نام بانک را وارد کنید:", reply_markup=_gateway_back_keyboard("admin:gw:card"))


@router.message(GatewaySettingsStates.waiting_for_card_bank)
async def card_bank_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    await AppSettingsRepository(session).update_gateway_settings(card_bank=message.text.strip())
    await state.clear()
    await message.answer("نام بانک ذخیره شد.")


@router.callback_query(F.data == "admin:gw:card_note")
async def card_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_card_note)
    await safe_edit_or_send(callback, "توضیح پرداخت را وارد کنید. برای حذف، کلمه حذف را بفرستید:", reply_markup=_gateway_back_keyboard("admin:gw:card"))


@router.message(GatewaySettingsStates.waiting_for_card_note)
async def card_note_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    note = message.text.strip()
    await AppSettingsRepository(session).update_gateway_settings(
        card_note=None if note == "حذف" else note
    )
    await state.clear()
    await message.answer("توضیح پرداخت ذخیره شد.")


@router.callback_query(F.data == "admin:settings:phone_verification")
async def phone_verification_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    settings = await AppSettingsRepository(session).get_phone_verification_settings()
    status = "فعال" if settings.enabled else "غیرفعال"
    mode = "فقط ایران" if settings.mode == "iran" else "هر شماره‌ای"
    text = (
        "تایید شماره موبایل\n\n"
        f"وضعیت: {status}\n"
        f"حالت پذیرش شماره: {mode}\n\n"
        "در صورت فعال بودن، کاربر قبل از خرید باید شماره موبایل خود را ارسال کند."
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="غیرفعال کردن" if settings.enabled else "فعال کردن",
        callback_data="admin:phone:toggle",
    )
    builder.button(text="فقط شماره ایران", callback_data="admin:phone:mode:iran")
    builder.button(text="هر شماره‌ای", callback_data="admin:phone:mode:any")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:phone:toggle")
async def phone_verification_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    settings = await repo.get_phone_verification_settings()
    await repo.update_phone_verification_settings(enabled=not settings.enabled)
    await phone_verification_menu(callback, session)


@router.callback_query(F.data.startswith("admin:phone:mode:"))
async def phone_verification_mode(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    mode = callback.data.rsplit(":", 1)[-1]
    await AppSettingsRepository(session).update_phone_verification_settings(mode=mode)
    await phone_verification_menu(callback, session)


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


def _gateway_back_keyboard(callback_data: str):
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.BACK, callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


def _mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only first 4 and last 4 chars."""
    if len(key) <= 10:
        return key[:2] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


# ─── Force Join Channel Settings ─────────────────────────────────────────────


@router.callback_query(F.data == "admin:settings:force_join")
async def force_join_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()

    status = "✅ فعال" if gw.force_join_enabled else "❌ غیرفعال"
    channel = gw.force_join_channel or "تنظیم نشده"

    text = (
        "📢 <b>جوین اجباری کانال</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 وضعیت: {status}\n"
        f"📢 کانال: <code>{channel}</code>\n\n"
        "⚠️ ربات باید ادمین کانال باشد تا بتواند عضویت را بررسی کند.\n"
        "آی‌دی کانال را به فرمت <code>@channel_username</code> یا <code>-100xxxx</code> وارد کنید."
    )

    builder = InlineKeyboardBuilder()
    toggle_text = "❌ غیرفعال کردن" if gw.force_join_enabled else "✅ فعال کردن"
    builder.button(text=toggle_text, callback_data="admin:fj:toggle")
    builder.button(text="📢 تغییر کانال", callback_data="admin:fj:set_channel")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:fj:toggle")
async def force_join_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    gw = await AppSettingsRepository(session).get_gateway_settings()
    new_state = not gw.force_join_enabled
    await AppSettingsRepository(session).update_gateway_settings(force_join_enabled=new_state)
    status = "فعال ✅" if new_state else "غیرفعال ❌"
    await safe_edit_or_send(callback, f"📢 جوین اجباری {status} شد.")

    # Re-show menu
    await force_join_menu(callback, session)


@router.callback_query(F.data == "admin:fj:set_channel")
async def force_join_set_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GatewaySettingsStates.waiting_for_force_join_channel)
    await safe_edit_or_send(
        callback,
        "📢 آی‌دی یا یوزرنیم کانال را ارسال کنید:\n\n"
        "مثال: <code>@mychannel</code> یا <code>-1001234567890</code>"
    )


@router.message(GatewaySettingsStates.waiting_for_force_join_channel)
async def force_join_set_channel_done(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    channel = message.text.strip()
    if not channel.startswith("@") and not channel.startswith("-"):
        await message.answer("❌ فرمت نامعتبر. باید با @ یا - شروع شود.")
        return

    await AppSettingsRepository(session).update_gateway_settings(force_join_channel=channel)
    await state.clear()
    await message.answer(f"✅ کانال جوین اجباری به <code>{channel}</code> تغییر کرد.")


# ─── Force Join Check (User callback) ────────────────────────────────────────
# This handler is for the "عضو شدم" button — registered on the MAIN router
# so it works even without admin middleware

from aiogram import Router as _Router
_force_join_check_router = _Router(name="force-join-check")


@_force_join_check_router.callback_query(F.data == "force_join:check")
async def force_join_check(callback: CallbackQuery, session: AsyncSession) -> None:
    """Check if user has joined the required channel."""
    if callback.from_user is None:
        return

    gw = await AppSettingsRepository(session).get_gateway_settings()
    if not gw.force_join_enabled or not gw.force_join_channel:
        await callback.answer("✅ عضویت تأیید شد!", show_alert=True)
        await safe_edit_or_send(callback, "✅ عضویت تأیید شد! لطفاً دوباره از منو استفاده کنید.")
        return

    try:
        member = await callback.bot.get_chat_member(
            chat_id=gw.force_join_channel.strip(),
            user_id=callback.from_user.id,
        )
        if member.status in ("member", "administrator", "creator"):
            await callback.answer("✅ عضویت تأیید شد!", show_alert=True)
            await safe_edit_or_send(callback, "✅ عضویت تأیید شد! لطفاً دوباره از منو استفاده کنید.")
        else:
            await callback.answer("❌ هنوز عضو کانال نشده‌اید!", show_alert=True)
    except Exception:
        await callback.answer("❌ خطا در بررسی عضویت. لطفاً دوباره تلاش کنید.", show_alert=True)
