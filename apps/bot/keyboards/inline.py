from __future__ import annotations

from decimal import Decimal
from math import ceil
from typing import Iterable
from uuid import UUID

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.texts import Buttons
from models.plan import Plan


def build_plan_selection_keyboard(plans: Iterable[Plan]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for plan in plans:
        builder.button(
            text=_format_plan_button_text(plan.name, plan.price, plan.currency),
            callback_data=f"plan:select:{plan.id}",
        )

    builder.adjust(1)
    return builder.as_markup()


def build_wallet_topup_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (5, 10, 20):
        builder.button(
            text=f"${amount}",
            callback_data=f"wallet:topup:preset:{amount}",
        )

    builder.button(
        text=Buttons.CUSTOM_AMOUNT,
        callback_data="wallet:topup:custom",
    )
    builder.adjust(3, 1)
    return builder.as_markup()


def build_wallet_profile_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=Buttons.TOPUP_CRYPTO, callback_data="wallet:topup")
    builder.adjust(1)
    return builder.as_markup()


def build_topup_link_keyboard(invoice_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
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
        text=Buttons.BACK,
        callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def _format_plan_button_text(name: str, price: Decimal, currency: str) -> str:
    return f"{name} - {price.normalize()} {currency}"


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
