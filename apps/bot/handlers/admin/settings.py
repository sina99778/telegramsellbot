from __future__ import annotations

import logging
import json

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import GatewaySettingsStates, ReferralSettingsStates, SalesChannelStates, SettingsStates
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
    user_actions = await settings_repo.get_user_actions_settings()
    toman_rate = await settings_repo.get_toman_rate()
    display_currency = await settings_repo.get_display_currency()
    
    text = admin_panel(
        "⚙️ تنظیمات عمومی ربات",
        [
            (
                "وضعیت فروش",
                [
                    ("فروش سرویس", status_label(user_actions.sales_enabled)),
                    ("تمدید سرویس", status_label(user_actions.renewals_enabled)),
                ],
            ),
            (
                "قیمت‌ها",
                [
                    ("تمدید هر ۱ گیگ", f"{renewal_settings.price_per_gb} دلار"),
                    ("تمدید هر ۱۰ روز", f"{renewal_settings.price_per_10_days} دلار"),
                    ("نرخ دلار", f"{toman_rate:,} تومان"),
                    ("ارز نمایش", "تومان 💵" if display_currency == "IRT" else "دلار $"),
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

    # Dynamic labels for sales/renewal toggles
    sales_label = "🔴 خاموش کردن فروش" if user_actions.sales_enabled else "🟢 روشن کردن فروش"
    renewal_label = "🔴 خاموش کردن تمدید" if user_actions.renewals_enabled else "🟢 روشن کردن تمدید"

    # Settings menu is grouped by area + visual section headers so admins
    # can scan to the relevant block instead of hunting through a flat list.
    builder = InlineKeyboardBuilder()

    # ── Section: Master switches (most-used toggles)
    builder.button(text="━━ 🚦 وضعیت فروش ━━", callback_data="admin:settings:noop")
    builder.button(text=sales_label, callback_data="admin:settings:toggle_sales")
    builder.button(text=renewal_label, callback_data="admin:settings:toggle_renewals")

    # ── Section: Pricing
    builder.button(text="━━ 💰 قیمت‌ها ━━", callback_data="admin:settings:noop")
    builder.button(text="قیمت گیگ تمدید", callback_data="admin:settings:edit_gb")
    builder.button(text="قیمت ۱۰ روز تمدید", callback_data="admin:settings:edit_days")
    builder.button(text="نرخ دلار", callback_data="admin:settings:edit_toman")
    currency_button_label = (
        "🔄 تغییر نمایش به دلار" if display_currency == "IRT"
        else "🔄 تغییر نمایش به تومان"
    )
    builder.button(text=currency_button_label, callback_data="admin:settings:toggle_currency")

    # ── Section: Custom plans
    builder.button(text="━━ 🧩 خرید دلخواه ━━", callback_data="admin:settings:noop")
    builder.button(text="فعال/غیرفعال خرید دلخواه", callback_data="admin:settings:custom_toggle")
    builder.button(text="قیمت گیگ دلخواه", callback_data="admin:settings:custom_gb")
    builder.button(text="قیمت روز دلخواه", callback_data="admin:settings:custom_day")

    # ── Section: Service security
    builder.button(text="━━ 🔒 امنیت سرویس ━━", callback_data="admin:settings:noop")
    builder.button(text="limitIp پنل", callback_data="admin:settings:xui_limit_ip")
    builder.button(text="سقف IP مجاز", callback_data="admin:settings:max_ips")
    builder.button(text="ضد اشتراک‌گذاری", callback_data="admin:settings:ip_guard_toggle")

    # ── Section: Gateways & onboarding
    builder.button(text="━━ 💳 درگاه و آن‌بوردینگ ━━", callback_data="admin:settings:noop")
    builder.button(text="درگاه‌ها", callback_data="admin:settings:gateways")
    builder.button(text="تایید موبایل", callback_data="admin:settings:phone_verification")
    builder.button(text="کانفیگ تست", callback_data="admin:settings:trial_toggle")
    builder.button(text="جوین اجباری", callback_data="admin:settings:force_join")
    builder.button(text="رفرال", callback_data="admin:settings:referral")

    # ── Section: Appearance
    builder.button(text="━━ 🎨 ظاهر ━━", callback_data="admin:settings:noop")
    builder.button(text="اموجی پریمیم", callback_data="admin:settings:premium_emoji_toggle")
    builder.button(text="📤 افزودن اموجی پریمیم", callback_data="admin:settings:premium_emoji_add")
    builder.button(text="📋 لیست اموجی‌ها", callback_data="admin:settings:premium_emoji_list")
    builder.button(text="🗑 پاک‌سازی همه", callback_data="admin:settings:premium_emoji_clear")
    builder.button(text="🎨 رنگ دکمه‌ها", callback_data="admin:settings:button_styles")

    # ── Section: Notifications
    builder.button(text="━━ 📢 اعلان‌ها ━━", callback_data="admin:settings:noop")
    builder.button(text="📢 کانال گزارش فروش", callback_data="admin:settings:sales_channel")

    # ── Section: Migration tools (one-off, but always available)
    builder.button(text="━━ 🚚 مهاجرت ━━", callback_data="admin:settings:noop")
    builder.button(text="📥 ایمپورت دیتابیس ربات قبلی", callback_data="admin:settings:legacy_import")

    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    # 1 header, 2 toggles, 1 header, 4 prices, 1 header, 3 custom,
    # 1 header, 3 security, 1 header, 5 gateways, 1 header, 5 appearance,
    # 1 header, 1 sales-channel, 1 header, 1 legacy-import, 1 back.
    builder.adjust(1, 2, 1, 4, 1, 3, 1, 3, 1, 5, 1, 5, 1, 1, 1, 1, 1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:noop")
async def settings_noop(callback: CallbackQuery) -> None:
    """Section-header buttons do nothing on click."""
    await callback.answer()


@router.callback_query(F.data == "admin:settings:toggle_sales")
async def toggle_sales(callback: CallbackQuery, session: AsyncSession) -> None:
    repo = AppSettingsRepository(session)
    current = await repo.get_user_actions_settings()
    new_val = not current.sales_enabled
    await repo.update_user_actions_settings(sales_enabled=new_val)
    status = "روشن ✅" if new_val else "خاموش 🔴"
    await callback.answer(f"فروش سرویس: {status}", show_alert=True)
    await bot_settings_menu(callback, session)


@router.callback_query(F.data == "admin:settings:toggle_renewals")
async def toggle_renewals(callback: CallbackQuery, session: AsyncSession) -> None:
    repo = AppSettingsRepository(session)
    current = await repo.get_user_actions_settings()
    new_val = not current.renewals_enabled
    await repo.update_user_actions_settings(renewals_enabled=new_val)
    status = "روشن ✅" if new_val else "خاموش 🔴"
    await callback.answer(f"تمدید سرویس: {status}", show_alert=True)
    await bot_settings_menu(callback, session)


@router.callback_query(F.data == "admin:settings:toggle_currency")
async def toggle_display_currency(callback: CallbackQuery, session: AsyncSession) -> None:
    """Flip the user-facing display between USD ($) and IRT (تومان).

    Internally we keep storing USD; this only changes what customers see
    in wallet balances, plan prices, topup invoices, etc. Conversion uses
    the existing toman-rate setting.
    """
    repo = AppSettingsRepository(session)
    current = await repo.get_display_currency()
    new_mode = "IRT" if current == "USD" else "USD"
    await repo.set_display_currency(new_mode)
    label = "تومان 💵" if new_mode == "IRT" else "دلار $"
    await callback.answer(f"نمایش ارز تغییر کرد به: {label}", show_alert=True)
    await bot_settings_menu(callback, session)


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


@router.callback_query(F.data == "admin:settings:premium_emoji_add")
async def edit_premium_emoji_map_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Show the operator-preferred "paste list + replace each emoji"
    flow. Every emoji actually used in the bot's user-facing text
    appears as `key=<default emoji>` — the admin replaces the right-hand
    side with their premium-emoji counterpart (or the raw custom_emoji_id
    if they have it) and sends it back.

    The submit handler accepts BOTH formats transparently — pure
    `<custom_emoji>` entities on the right and raw IDs both work.
    """
    from services.telegram.premium_emoji import DEFAULT_EMOJI_KEYS

    await callback.answer()
    await state.set_state(SettingsStates.waiting_for_premium_emoji_map)

    settings_repo = AppSettingsRepository(session)
    current_settings = await settings_repo.get_premium_emoji_settings()
    current_count = len(current_settings.emoji_map or {})

    # Build the catalogue body. Each line is `key=<default emoji>`.
    catalogue_lines = [f"{k}={v}" for k, v in DEFAULT_EMOJI_KEYS.items()]
    body = "\n".join(catalogue_lines)

    header = (
        "✨ <b>مدیریت آسان اموجی‌های پرمیوم</b>\n\n"
        f"📊 اموجی‌های فعلی نگاشته‌شده: <b>{current_count}</b> از <b>{len(DEFAULT_EMOJI_KEYS)}</b>\n\n"
        "برای تغییر اموجی‌ها، کافیست لیست زیر را کپی کنید، اموجی پیش‌فرض را پاک کرده و اموجی پرمیوم خود را جایگزین کنید "
        "(یا آیدی آن را بگذارید) و بفرستید:\n\n"
    )
    footer = "\n\nبرای لغو /cancel را بفرستید."

    # Telegram message limit is 4096 chars. Our catalogue is ~110 lines
    # × ~15 chars ≈ 1700 chars — safely fits. Wrap in <pre> so the
    # operator can long-press to copy on mobile.
    msg = header + f"<pre>{body}</pre>" + footer
    await safe_edit_or_send(callback, msg, parse_mode="HTML")


@router.message(SettingsStates.waiting_for_premium_emoji_map)
async def edit_premium_emoji_map_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Parse the operator's reply. Three modes are accepted transparently:

    1. **Paste-list with replaced emojis** (preferred):
            success=<premium ✅>
            error=<premium ❌>
            ...
       — for each line, we look at the byte range AFTER the `=` and pick
       any `custom_emoji` MessageEntity that falls inside it.

    2. **Paste-list with raw IDs**:
            success=5210952531676504517
            error=5197488032259546995
       — the right side is a numeric custom_emoji_id we keep as-is.

    3. **Single-message with just premium emojis** (no key=):
            ✨🔥💎
       — falls back to "auto-map each entity to its underlying fallback
       char" behaviour from the previous design.

    Mode 1 wins when both keys + entities are present, because the
    explicit key→value pairing is more deterministic than auto-detection.
    """
    from services.telegram.premium_emoji import (
        VALID_CUSTOM_EMOJI_ID_RE,
        clear_premium_emoji_cache,
    )

    text = message.text or message.caption or ""
    entities = list(message.entities or message.caption_entities or [])
    utf16 = text.encode("utf-16-le") if text else b""

    extracted: dict[str, str] = {}

    # ── Mode 1 + 2: paste-list parsing ────────────────────────────
    # For each `key=value` line, locate it inside the original text
    # (utf-16 indexed) so we can correlate any custom_emoji entity that
    # falls inside the value's byte range.
    cursor_utf16 = 0
    if text:
        for raw_line in text.split("\n"):
            line_len_utf16 = len(raw_line.encode("utf-16-le")) // 2
            sep = "=" if "=" in raw_line else (":" if ":" in raw_line else None)
            if not sep:
                cursor_utf16 += line_len_utf16 + 1  # +1 for the \n
                continue

            # Where does the value start within the whole message, in UTF-16 units?
            key_part, val_part = raw_line.split(sep, 1)
            key = key_part.strip()
            if not key:
                cursor_utf16 += line_len_utf16 + 1
                continue

            val_start_utf16 = cursor_utf16 + (len((key_part + sep).encode("utf-16-le")) // 2)
            val_end_utf16 = cursor_utf16 + line_len_utf16

            # Any custom_emoji entity overlapping the value range?
            chosen_cid: str | None = None
            for ent in entities:
                if ent.type != "custom_emoji" or not ent.custom_emoji_id:
                    continue
                if ent.offset < val_start_utf16 or ent.offset >= val_end_utf16:
                    continue
                cid = str(ent.custom_emoji_id).strip()
                if VALID_CUSTOM_EMOJI_ID_RE.match(cid):
                    chosen_cid = cid
                    break

            if chosen_cid:
                extracted[key] = chosen_cid
            else:
                # No entity → try raw ID after `=`.
                val_clean = val_part.strip()
                if val_clean and VALID_CUSTOM_EMOJI_ID_RE.match(val_clean):
                    extracted[key] = val_clean

            cursor_utf16 += line_len_utf16 + 1  # account for \n

    # ── Mode 3: single-message of just premium emojis ─────────────
    # Used only if the operator didn't include a single key=. We auto-
    # map each premium emoji to its fallback character.
    if not extracted and text and entities:
        for ent in entities:
            if ent.type != "custom_emoji" or not ent.custom_emoji_id:
                continue
            try:
                slice_bytes = utf16[ent.offset * 2 : (ent.offset + ent.length) * 2]
                fallback = slice_bytes.decode("utf-16-le")
            except Exception:
                continue
            fallback = fallback.strip()
            if not fallback:
                continue
            cid = str(ent.custom_emoji_id).strip()
            if VALID_CUSTOM_EMOJI_ID_RE.match(cid):
                extracted[fallback] = cid

    if not extracted:
        await message.answer(
            "⚠️ هیچ اموجی پریمیومی توی پیام پیدا نشد.\n\n"
            "<b>دو روش پشتیبانی می‌شه:</b>\n"
            "1) لیست رو کپی کن، جای هر اموجی پیش‌فرض، اموجی پرمیوم خودت رو بذار:\n"
            "   <code>success=&lt;premium emoji&gt;</code>\n"
            "2) فقط چند تا اموجی پرمیوم رو بفرست — ربات خودش به اموجی پایه‌ی هرکدوم وصل می‌کنه:\n"
            "   <code>✨🔥💎</code>\n\n"
            "<i>برای لغو /cancel را بفرست.</i>",
            parse_mode="HTML",
        )
        return

    settings_repo = AppSettingsRepository(session)
    current_settings = await settings_repo.get_premium_emoji_settings()
    new_map = dict(current_settings.emoji_map or {})
    new_map.update(extracted)

    await settings_repo.update_premium_emoji_settings(enabled=True, emoji_map=new_map)
    clear_premium_emoji_cache()
    await state.clear()

    # Build a result list showing which emojis got mapped, so the admin
    # has visual confirmation. The pre-tag prevents Telegram from
    # re-rendering the just-saved premium emojis in this confirmation.
    sample = list(extracted.items())[:12]
    lines = [
        "✅ <b>اموجی‌های پریمیوم ذخیره شدن</b>",
        f"📥 این پیام: <b>{len(extracted)}</b>",
        f"📊 کل اموجی‌های فعال: <b>{len(new_map)}</b>",
        "",
        "<b>نگاشت‌های جدید:</b>",
    ]
    for fb, cid in sample:
        lines.append(f"  <code>{fb}</code> → <code>{cid}</code>")
    if len(extracted) > len(sample):
        lines.append(f"  … و {len(extracted) - len(sample)} مورد دیگر")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:premium_emoji_list")
async def list_premium_emoji_map(callback: CallbackQuery, session: AsyncSession) -> None:
    """Show the current map so the admin can audit what's set."""
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    current_settings = await settings_repo.get_premium_emoji_settings()
    emoji_map = current_settings.emoji_map or {}

    from services.telegram.premium_emoji import DEFAULT_EMOJI_KEYS

    if not emoji_map:
        await safe_edit_or_send(
            callback,
            "📋 <b>لیست نگاشت اموجی پریمیم</b>\n\n"
            "هنوز هیچ اموجی پریمیومی ثبت نشده.\n"
            "از منوی تنظیمات «📤 افزودن اموجی پریمیم» را بزن.",
            parse_mode="HTML",
        )
        return

    lines = ["📋 <b>لیست نگاشت اموجی پریمیم</b>", ""]
    # First show entries whose key is already an emoji (the new format)
    for key, cid in emoji_map.items():
        if key in DEFAULT_EMOJI_KEYS:
            fb = DEFAULT_EMOJI_KEYS[key]
            lines.append(f"  <code>{fb}</code> ({key}) → <code>{cid}</code>")
        else:
            lines.append(f"  <code>{key}</code> → <code>{cid}</code>")

    lines.append("")
    lines.append(f"📊 جمعاً: <b>{len(emoji_map)}</b> نگاشت")
    await safe_edit_or_send(callback, "\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:premium_emoji_clear")
async def clear_premium_emoji_map(callback: CallbackQuery, session: AsyncSession) -> None:
    """Wipe the map completely. Useful when the admin wants to start over."""
    from services.telegram.premium_emoji import clear_premium_emoji_cache

    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    await settings_repo.update_premium_emoji_settings(emoji_map={})
    clear_premium_emoji_cache()
    await safe_edit_or_send(
        callback,
        "🗑 لیست نگاشت اموجی‌های پریمیم پاک شد.",
        parse_mode="HTML",
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
    repo = AppSettingsRepository(session)
    gw = await repo.get_gateway_settings()
    auto = await repo.get_card_autoconfirm_settings()
    status = "فعال" if gw.card_to_card_enabled else "غیرفعال"
    auto_label = (
        f"🟢 بعد از {auto.delay_minutes} دقیقه" if auto.enabled
        else "🔴 خاموش"
    )
    text = (
        "مدیریت کارت به کارت\n\n"
        f"وضعیت: {status}\n"
        f"شماره کارت: <code>{gw.card_number or 'تنظیم نشده'}</code>\n"
        f"صاحب کارت: {gw.card_holder or 'تنظیم نشده'}\n"
        f"بانک: {gw.card_bank or 'تنظیم نشده'}\n"
        f"توضیح: {gw.card_note or 'تنظیم نشده'}\n\n"
        f"تأیید خودکار رسید: {auto_label}\n"
        f"کاربران مستثنا: {len(auto.exception_telegram_ids)} نفر"
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
    builder.button(text="⏱ تأیید خودکار رسید", callback_data="admin:gw:card_auto")
    builder.button(text=AdminButtons.BACK, callback_data="admin:settings:gateways")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─── Card auto-confirm submenu ──────────────────────────────────────────


class _CardAutoMinutesState(StatesGroup):
    waiting_for_minutes = State()
    waiting_for_exception_ids = State()


@router.callback_query(F.data == "admin:gw:card_auto")
async def card_auto_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    auto = await repo.get_card_autoconfirm_settings()
    exempt_preview = ", ".join(str(x) for x in auto.exception_telegram_ids[:8]) or "—"
    if len(auto.exception_telegram_ids) > 8:
        exempt_preview += f" … (+{len(auto.exception_telegram_ids) - 8})"
    text = (
        "⏱ <b>تأیید خودکار رسید کارت‌به‌کارت</b>\n\n"
        f"وضعیت: {'🟢 روشن' if auto.enabled else '🔴 خاموش'}\n"
        f"تأخیر: <b>{auto.delay_minutes}</b> دقیقه پس از ثبت رسید\n"
        f"کاربران مستثنا: <b>{len(auto.exception_telegram_ids)}</b>\n"
        f"<code>{_html_escape(exempt_preview)}</code>\n\n"
        "<i>وقتی روشن باشد، رسیدی که در حالت «در انتظار تأیید» مانده و از مدت تنظیم‌شده گذشته،"
        " به‌طور خودکار تأیید و کیف پول کاربر شارژ می‌شود — مگر اینکه آی‌دی کاربر در لیست استثنا باشد.</i>"
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("🔴 خاموش کن" if auto.enabled else "🟢 روشن کن"),
        callback_data="admin:gw:card_auto:toggle",
    )
    builder.button(text=f"⏱ تأخیر ({auto.delay_minutes} دقیقه)",
                   callback_data="admin:gw:card_auto:minutes")
    builder.button(text="🚫 ویرایش لیست استثنا",
                   callback_data="admin:gw:card_auto:exceptions")
    builder.button(text=AdminButtons.BACK, callback_data="admin:gw:card")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


def _html_escape(s: str) -> str:
    from html import escape as _e
    return _e(s)


@router.callback_query(F.data == "admin:gw:card_auto:toggle")
async def card_auto_toggle(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    auto = await repo.get_card_autoconfirm_settings()
    await repo.update_card_autoconfirm_settings(enabled=not auto.enabled)
    await card_auto_menu(callback, session)


@router.callback_query(F.data == "admin:gw:card_auto:minutes")
async def card_auto_minutes_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(_CardAutoMinutesState.waiting_for_minutes)
    await safe_edit_or_send(
        callback,
        "⏱ تعداد دقیقه‌ی تأخیر تا تأیید خودکار را وارد کن (عددی ≥ ۱).\n\n"
        "مثال: <code>30</code>\n\nبرای لغو /cancel را بفرست.",
        parse_mode="HTML",
    )


@router.message(_CardAutoMinutesState.waiting_for_minutes)
async def card_auto_minutes_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text or message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("لغو شد.")
        return
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ عدد نامعتبر. یک عدد ≥ ۱ بفرست.")
        return
    repo = AppSettingsRepository(session)
    await repo.update_card_autoconfirm_settings(delay_minutes=minutes)
    await state.clear()
    await message.answer(f"✅ تأخیر تأیید خودکار رسید روی <b>{minutes}</b> دقیقه تنظیم شد.", parse_mode="HTML")


@router.callback_query(F.data == "admin:gw:card_auto:exceptions")
async def card_auto_exceptions_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(_CardAutoMinutesState.waiting_for_exception_ids)
    await safe_edit_or_send(
        callback,
        "🚫 لیست telegram_id کاربرانی که نباید رسید آن‌ها خودکار تأیید شود را بفرست.\n"
        "می‌توانی با کاما، فاصله یا خط جدید جدا کنی. برای پاک کردن لیست، خالی بفرست.\n\n"
        "مثال: <code>123456789, 987654321</code>\n\nبرای لغو /cancel را بفرست.",
        parse_mode="HTML",
    )


@router.message(_CardAutoMinutesState.waiting_for_exception_ids)
async def card_auto_exceptions_submit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if raw == "/cancel":
        await state.clear()
        await message.answer("لغو شد.")
        return
    # Accept comma/space/newline separated. Filter to positive ints.
    ids: list[int] = []
    if raw and raw != "-":
        import re as _re
        for tok in _re.split(r"[,\s\n]+", raw):
            tok = tok.strip()
            if not tok:
                continue
            try:
                v = int(tok)
                if v > 0:
                    ids.append(v)
            except ValueError:
                pass
    repo = AppSettingsRepository(session)
    await repo.update_card_autoconfirm_settings(exception_telegram_ids=ids)
    await state.clear()
    await message.answer(f"✅ لیست استثنا با <b>{len(ids)}</b> آی‌دی به‌روزرسانی شد.", parse_mode="HTML")


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


# ─── Sales report channel ───────────────────────────────────────────────────
#
# Optional dedicated chat (channel / supergroup) that every purchase,
# renewal and topup notification is routed to instead of DM'ing every
# admin. Saves admins from a flood of pings and gives the team one
# shared activity feed.

@router.callback_query(F.data == "admin:settings:sales_channel")
async def sales_channel_overview(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show current sales-channel state + options to set / clear / test."""
    await callback.answer()
    settings_repo = AppSettingsRepository(session)
    chat_id = await settings_repo.get_sales_report_chat_id()

    builder = InlineKeyboardBuilder()
    if chat_id is None:
        text = (
            "📢 <b>کانال گزارش فروش</b>\n"
            "━━━━━━━━━━━━━━\n"
            "❌ هیچ کانالی تنظیم نشده — اعلان‌ها به DM همه‌ی ادمین‌ها می‌رود.\n\n"
            "<b>برای تنظیم:</b>\n"
            "1) ربات را به‌عنوان <b>ادمین</b> به کانال خود اضافه کنید "
            "(دسترسی «ارسال پیام» کافی است).\n"
            "2) یک پیام از همان کانال را به ربات <b>فوروارد</b> کنید "
            "یا chat_id کانال را به‌صورت دستی بفرستید "
            "(مثل <code>-1001234567890</code>)."
        )
        builder.button(text="➕ تنظیم کانال", callback_data="admin:settings:sales_channel:set")
    else:
        text = (
            "📢 <b>کانال گزارش فروش</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"✅ chat_id فعلی: <code>{chat_id}</code>\n\n"
            "هر خرید، تمدید و شارژ کیف پول به این کانال ارسال می‌شود.\n"
            "اعلان‌های اضطراری (شکست refund و …) همچنان به DM ادمین‌ها می‌رود."
        )
        builder.button(text="🧪 ارسال پیام تست", callback_data="admin:settings:sales_channel:test")
        builder.button(text="🔁 تغییر کانال", callback_data="admin:settings:sales_channel:set")
        builder.button(text="🗑 حذف کانال", callback_data="admin:settings:sales_channel:clear")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1)
    await state.clear()
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:sales_channel:set")
async def sales_channel_set_prompt(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SalesChannelStates.waiting_for_channel)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ انصراف", callback_data="admin:settings:sales_channel")
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "📢 <b>تنظیم کانال گزارش فروش</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>روش ۱:</b> یک پیام از کانال هدف را به همین چت <b>فوروارد</b> کنید "
        "(ربات chat_id را خودش استخراج می‌کند).\n\n"
        "<b>روش ۲:</b> chat_id کانال را مستقیماً بفرستید "
        "(عدد منفی مثل <code>-1001234567890</code>).\n\n"
        "⚠️ ربات باید از قبل به‌عنوان ادمین/عضو کانال اضافه شده باشد، "
        "وگرنه ارسال پیام شکست می‌خورد.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.message(SalesChannelStates.waiting_for_channel)
async def sales_channel_receive(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Accept either a forwarded message OR a raw chat_id."""
    chat_id: int | None = None
    title: str | None = None

    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        chat_id = fwd_chat.id
        title = fwd_chat.title or fwd_chat.username or None
    elif message.text:
        raw = message.text.strip()
        try:
            chat_id = int(raw)
        except ValueError:
            await message.answer(
                "❌ ورودی معتبر نیست. یک پیام را از کانال هدف فوروارد کنید "
                "یا chat_id را به‌صورت یک عدد صحیح بفرستید."
            )
            return

    if chat_id is None:
        await message.answer(
            "❌ ورودی معتبر نیست. یک پیام را از کانال هدف فوروارد کنید "
            "یا chat_id را به‌صورت یک عدد صحیح بفرستید."
        )
        return

    # Sanity-test the channel by actually sending a message. If the bot
    # isn't in the channel, we want the admin to know NOW, not the first
    # time a customer buys.
    try:
        await message.bot.send_message(
            chat_id,
            "✅ این چت به‌عنوان <b>کانال گزارش فروش</b> ربات تنظیم شد.\n"
            "از این به بعد، خریدها/تمدیدها/شارژها اینجا گزارش خواهند شد.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(
            f"❌ ربات نتوانست به <code>{chat_id}</code> پیام بفرستد:\n"
            f"<code>{type(exc).__name__}: {str(exc)[:200]}</code>\n\n"
            "ابتدا ربات را به‌عنوان ادمین در آن کانال اضافه کنید و دوباره تلاش کنید."
        )
        return

    await AppSettingsRepository(session).set_sales_report_chat_id(chat_id, title=title)
    await state.clear()
    await message.answer(
        f"✅ کانال گزارش فروش روی <code>{chat_id}</code> تنظیم شد.\n"
        "اعلان‌های فروش از این لحظه به آنجا می‌روند."
    )


@router.callback_query(F.data == "admin:settings:sales_channel:test")
async def sales_channel_test(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """Fire a sample message into the configured channel to verify routing."""
    await callback.answer()
    chat_id = await AppSettingsRepository(session).get_sales_report_chat_id()
    if chat_id is None:
        await callback.answer("هیچ کانالی تنظیم نشده.", show_alert=True)
        return
    try:
        await callback.bot.send_message(
            chat_id,
            "🧪 پیام تست از پنل ادمین — کانال گزارش فروش به‌درستی کار می‌کند.",
            parse_mode="HTML",
        )
        await callback.answer("✅ پیام تست ارسال شد.", show_alert=True)
    except Exception as exc:
        await callback.answer(
            f"❌ خطا: {type(exc).__name__}: {str(exc)[:120]}",
            show_alert=True,
        )


@router.callback_query(F.data == "admin:settings:sales_channel:clear")
async def sales_channel_clear(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer("✅ حذف شد. اعلان‌ها به DM ادمین‌ها برمی‌گردد.", show_alert=True)
    await AppSettingsRepository(session).set_sales_report_chat_id(None)
    await sales_channel_overview(callback, state, session)


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


# ─── Button-style (Bot API 9.4 inline button colors) ───────────────────────

# Cycle order when admin taps a role row. We include "" to allow opting
# out of coloring for a particular role.
_BUTTON_STYLE_CYCLE = ("primary", "success", "danger", "")
_BUTTON_STYLE_LABEL = {
    "primary": "🔵 آبی (Primary)",
    "success": "🟢 سبز (Success)",
    "danger":  "🔴 قرمز (Danger)",
    "":        "⚪️ بدون رنگ",
}
_BUTTON_ROLE_LABEL = {
    "confirm": "✅ تأیید / مالی",
    "destructive": "🗑 خطرناک / تنظیمات",
    "navigation": "🔙 بازگشت / پیمایش",
    "info": "ℹ️ نمایش / مدیریت",
}


def _next_style(current: str) -> str:
    try:
        i = _BUTTON_STYLE_CYCLE.index(current)
    except ValueError:
        i = -1
    return _BUTTON_STYLE_CYCLE[(i + 1) % len(_BUTTON_STYLE_CYCLE)]


async def _render_button_styles_panel(callback: CallbackQuery, session: AsyncSession) -> None:
    repo = AppSettingsRepository(session)
    cfg = await repo.get_button_style_settings()

    text = admin_panel(
        "🎨 رنگ دکمه‌های ربات",
        [
            (
                "وضعیت",
                [("فعال", status_label(cfg.enabled))],
            ),
            (
                "نگاشت نقش‌ها به رنگ",
                [
                    (_BUTTON_ROLE_LABEL["confirm"],     _BUTTON_STYLE_LABEL.get(cfg.confirm, cfg.confirm)),
                    (_BUTTON_ROLE_LABEL["destructive"], _BUTTON_STYLE_LABEL.get(cfg.destructive, cfg.destructive)),
                    (_BUTTON_ROLE_LABEL["navigation"],  _BUTTON_STYLE_LABEL.get(cfg.navigation, cfg.navigation)),
                    (_BUTTON_ROLE_LABEL["info"],        _BUTTON_STYLE_LABEL.get(cfg.info, cfg.info)),
                ],
            ),
            (
                "راهنما",
                [
                    ("نیاز", "نسخه تلگرام با Bot API 9.4 (Feb 2026)"),
                    ("اثر", "روی دکمه‌های شیشه‌ای پنل ادمین"),
                ],
            ),
        ],
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text=("🔴 خاموش کن" if cfg.enabled else "🟢 روشن کن"),
        callback_data="admin:settings:button_styles:toggle",
    )
    builder.button(text=f"تأیید: {_BUTTON_STYLE_LABEL.get(cfg.confirm)}",
                   callback_data="admin:settings:button_styles:cycle:confirm")
    builder.button(text=f"خطرناک: {_BUTTON_STYLE_LABEL.get(cfg.destructive)}",
                   callback_data="admin:settings:button_styles:cycle:destructive")
    builder.button(text=f"پیمایش: {_BUTTON_STYLE_LABEL.get(cfg.navigation)}",
                   callback_data="admin:settings:button_styles:cycle:navigation")
    builder.button(text=f"نمایش: {_BUTTON_STYLE_LABEL.get(cfg.info)}",
                   callback_data="admin:settings:button_styles:cycle:info")
    builder.button(text="↩ پیش‌فرض‌ها", callback_data="admin:settings:button_styles:reset")
    builder.button(text=AdminButtons.BACK, callback_data="admin:bot_settings")
    builder.adjust(1, 1, 1, 1, 1, 1, 1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:button_styles")
async def open_button_styles_panel(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await _render_button_styles_panel(callback, session)


@router.callback_query(F.data == "admin:settings:button_styles:toggle")
async def toggle_button_styles(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    repo = AppSettingsRepository(session)
    cfg = await repo.get_button_style_settings()
    await repo.update_button_style_settings(enabled=not cfg.enabled)
    from apps.bot.utils.button_style import clear_button_style_cache, prime_button_style_cache
    clear_button_style_cache()
    await prime_button_style_cache()
    await _render_button_styles_panel(callback, session)


@router.callback_query(F.data.startswith("admin:settings:button_styles:cycle:"))
async def cycle_button_style_role(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    role = (callback.data or "").rsplit(":", 1)[-1]
    if role not in {"confirm", "destructive", "navigation", "info"}:
        return
    repo = AppSettingsRepository(session)
    cfg = await repo.get_button_style_settings()
    new_style = _next_style(getattr(cfg, role))
    await repo.update_button_style_settings(**{role: new_style})
    from apps.bot.utils.button_style import clear_button_style_cache, prime_button_style_cache
    clear_button_style_cache()
    await prime_button_style_cache()
    await _render_button_styles_panel(callback, session)


@router.callback_query(F.data == "admin:settings:button_styles:reset")
async def reset_button_styles(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer("به پیش‌فرض‌ها برگشت.", show_alert=False)
    repo = AppSettingsRepository(session)
    await repo.update_button_style_settings(
        enabled=True, confirm="success", destructive="danger",
        navigation="primary", info="primary",
    )
    from apps.bot.utils.button_style import clear_button_style_cache, prime_button_style_cache
    clear_button_style_cache()
    await prime_button_style_cache()
    await _render_button_styles_panel(callback, session)
