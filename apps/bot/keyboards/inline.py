from __future__ import annotations

from decimal import Decimal
from math import ceil
from typing import Iterable, Mapping
from uuid import UUID

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.formatting import format_money
from core.texts import Buttons
from models.plan import Plan


def build_plan_selection_keyboard(
    plans: Iterable[Plan],
    stock_by_plan_id: Mapping[UUID, object] | None = None,
    *,
    include_custom_purchase: bool = False,
    display_mode: str = "USD",
    toman_rate: int = 100000,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if include_custom_purchase:
        builder.button(
            text="🧩 خرید حجم و زمان دلخواه",
            callback_data="purchase:custom",
        )

    # Pick a "Recommended" plan: lowest price-per-day. Helps first-time buyers
    # who have nothing to anchor to and would otherwise default to the cheapest
    # (often least valuable) option.
    plan_list = list(plans)
    best_plan_id = None
    if plan_list:
        def _ppd(p: Plan) -> float:
            try:
                return float(p.price) / max(1, int(getattr(p, "duration_days", 1) or 1))
            except Exception:
                return float("inf")
        best_plan_id = min(plan_list, key=_ppd).id

    for plan in plan_list:
        stock = stock_by_plan_id.get(plan.id) if stock_by_plan_id else None
        stock_label = _format_stock_label(stock)
        recommended = plan.id == best_plan_id
        builder.button(
            text=_format_plan_button_text(
                plan.name, plan.price, plan.currency, stock_label,
                volume_bytes=getattr(plan, "volume_bytes", 0),
                duration_days=getattr(plan, "duration_days", 0),
                recommended=recommended,
                display_mode=display_mode,
                toman_rate=toman_rate,
            ),
            callback_data=f"plan:select:{plan.id}",
        )
    builder.button(text="❌ انصراف", callback_data="purchase:cancel")
    builder.adjust(1)
    return builder.as_markup()


def build_wallet_topup_keyboard(presets: list[int] | tuple[int, ...] | None = None) -> InlineKeyboardMarkup:
    """Wallet topup keyboard.

    ``presets`` lets the caller pass admin-configured topup amounts so each
    deployment can match its local market (a $5 preset is useless in a
    region where the typical topup is $50). Falls back to a sane default.
    """
    builder = InlineKeyboardBuilder()
    amounts = list(presets) if presets else [5, 10, 20, 50]
    # Filter, dedupe and clamp; keep at most 6 buttons to avoid scrolling.
    amounts = sorted({int(a) for a in amounts if isinstance(a, (int, float)) and a > 0})[:6]
    if not amounts:
        amounts = [5, 10, 20]
    for amount in amounts:
        builder.button(
            text=f"${amount}",
            callback_data=f"wallet:topup:preset:{amount}",
        )

    builder.button(
        text=Buttons.CUSTOM_AMOUNT,
        callback_data="wallet:topup:custom",
    )
    builder.button(text=Buttons.BACK, callback_data="wallet:profile")
    # Layout: 3 amount buttons per row, then custom, then back.
    rows = [3] * (len(amounts) // 3)
    if len(amounts) % 3:
        rows.append(len(amounts) % 3)
    rows += [1, 1]
    builder.adjust(*rows)
    return builder.as_markup()


def build_wallet_profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 شارژ حساب", callback_data="wallet:topup")
    builder.button(text="📊 تاریخچه تراکنش‌ها", callback_data="wallet:history")
    builder.button(text="❌ بستن", callback_data="purchase:cancel")
    builder.adjust(1)
    return builder.as_markup()

def build_wallet_history_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=Buttons.BACK, callback_data="wallet:profile")
    return builder.as_markup()


def build_gateway_selection_keyboard(
    nowpayments_enabled: bool = True,
    tetrapay_enabled: bool = True,
    tronado_enabled: bool = False,
    manual_crypto_enabled: bool = False,
    manual_wallets: list[dict[str, str]] | None = None,
    card_to_card_enabled: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if tetrapay_enabled:
        builder.button(text="💳 درگاه ریالی (تتراپی)", callback_data="wallet:topup:pay:tetrapay")
    if tronado_enabled:
        builder.button(text="درگاه ترونادو", callback_data="wallet:topup:pay:tronado")
    if nowpayments_enabled:
        builder.button(text="💎 درگاه ارزی (NOWPayments)", callback_data="wallet:topup:pay:gateway")
    if manual_crypto_enabled and (manual_wallets or []):
        for index, wallet in enumerate(manual_wallets or []):
            currency = wallet.get("currency") or "Crypto"
            builder.button(text=f"ðŸ’° {currency}", callback_data=f"wallet:topup:pay:manual:{index}")
    if manual_crypto_enabled and not (manual_wallets or []):
        builder.button(text="💰 پرداخت به ولت (دستی)", callback_data="wallet:topup:pay:manual")
    if card_to_card_enabled:
        builder.button(text="کارت به کارت", callback_data="wallet:topup:pay:card")
    if not tetrapay_enabled and not tronado_enabled and not nowpayments_enabled and not manual_crypto_enabled and not card_to_card_enabled:
        # No gateways available — show a disabled placeholder
        builder.button(text="❌ درگاه پرداختی فعال نیست", callback_data="pagination:noop")
    builder.button(text=Buttons.BACK, callback_data="wallet:topup")
    builder.adjust(1)
    return builder.as_markup()


def build_topup_link_keyboard(invoice_url: str, bot_url: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if bot_url:
        builder.button(text="💳 پرداخت در تتراپی", url=bot_url)
    else:
        builder.button(text=Buttons.OPEN_PAYMENT, url=invoice_url)
    builder.adjust(1)
    return builder.as_markup()


def build_renewal_keyboard(sub_id: UUID) -> InlineKeyboardMarkup:
    from apps.bot.handlers.user.renewal import RenewTypeCallback
    from apps.bot.handlers.user.my_configs import MyConfigCallback

    builder = InlineKeyboardBuilder()
    builder.button(
        text=Buttons.RENEW_VOLUME,
        callback_data=RenewTypeCallback(type="volume", sub_id=sub_id).pack(),
    )
    builder.button(
        text=Buttons.RENEW_TIME,
        callback_data=RenewTypeCallback(type="time", sub_id=sub_id).pack(),
    )
    builder.button(
        text="📦 تمدید کل پلن",
        callback_data=RenewTypeCallback(type="plan", sub_id=sub_id).pack(),
    )
    builder.button(
        text=Buttons.BACK,
        callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def _format_plan_button_text(
    name: str,
    price: Decimal,
    currency: str,
    stock_label: str = "",
    *,
    volume_bytes: int = 0,
    duration_days: int = 0,
    recommended: bool = False,
    display_mode: str = "USD",
    toman_rate: int = 100000,
) -> str:
    """Plan button text: includes volume + duration so the user doesn't
    have to open the plan just to see what they're getting."""
    bits: list[str] = []
    if volume_bytes:
        gb = volume_bytes / (1024 ** 3)
        bits.append(f"{gb:.0f}GB" if gb >= 1 else f"{volume_bytes // (1024 ** 2)}MB")
    if duration_days:
        # Show months when it makes sense, otherwise days.
        if duration_days >= 30 and duration_days % 30 == 0:
            months = duration_days // 30
            bits.append(f"{months} ماه")
        else:
            bits.append(f"{duration_days} روز")
    # Render the price in the operator's chosen display currency (USD or Toman)
    # so the plan list matches every other price the customer sees.
    bits.append(format_money(price, mode=display_mode, toman_rate=toman_rate))
    summary = " • ".join(bits)
    prefix = "⭐ " if recommended else ""
    suffix = f" • {stock_label}" if stock_label else ""
    return f"{prefix}{name} — {summary}{suffix}"


def _format_stock_label(stock: object | None) -> str:
    if stock is None or getattr(stock, "is_unlimited", True):
        return ""
    remaining = getattr(stock, "stock_remaining", None)
    if remaining is None:
        return ""
    return f"موجودی: {remaining}"


def add_pagination_controls(
    builder: InlineKeyboardBuilder,
    *,
    page: int,
    total_items: int,
    page_size: int,
    prev_callback_data: str,
    next_callback_data: str,
) -> None:
    total_pages = max(ceil(total_items / page_size), 1)
    if total_pages <= 1:
        return

    if page > 1:
        builder.button(text=Buttons.PREV, callback_data=prev_callback_data)
    builder.button(text=f"{page}/{total_pages}", callback_data="pagination:noop")
    if page < total_pages:
        builder.button(text=Buttons.NEXT, callback_data=next_callback_data)
