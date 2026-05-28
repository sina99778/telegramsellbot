"""
Structured sales-report notifications.

Replaces the ad-hoc flat strings each handler used to build itself.
Every call into this module produces the polished, sectioned format
the operator asked for (matching the reference screenshot they sent):

    🛒 | خرید جدید ( کیف پول )
    ━━━━━━━━━━━━━━━━━━━━━

    💬 مشخصات کاربر
    🪪 آیدی کاربر: 1788029280
    👤 اسم کاربر: Soran.D
    💬 نام کاربری: @soran_Dogohari
    ⚡ شماره تماس: 989126100752
    💰 موجودی کاربر: 951,250 تومان

    💬 مشخصات سرویس
    🚦 سرور: تانل انگلیس
    📌 نام سرویس: S3-81963
    💾 حجم سرویس: 30 گیگ
    ⏰ مدت سرویس: 30 روز
    💵 مبلغ پرداختی: 50,000 تومان

    1404/10/07 18:08:08

    بنازم خرید جدید ❤️

Public surface
--------------
    await notify_purchase(session, bot, user=…, subscription=…, plan=…,
                          price_usd=…, payment_method=…, config_name=…)
    await notify_renewal( session, bot, user=…, subscription=…,
                          renew_type=…, amount=…, price_usd=…,
                          payment_method=…, server_label=…)
    await notify_wallet_topup(session, bot, user=…, amount_usd=…,
                              payment_method=…, tx_hash=…)

Routing: every helper ultimately calls `notify_sales_event` (existing)
which respects the operator's sales-report channel + admin-DM fallback.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from core.formatting import format_money
from core.jalali import format_jalali
from models.subscription import Subscription
from models.user import User
from repositories.settings import AppSettingsRepository
from services.notifications import notify_sales_event


logger = logging.getLogger(__name__)


_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━"


# ── Method labels ───────────────────────────────────────────────────────


_METHOD_LABELS_FA: dict[str, str] = {
    "wallet":          "کیف پول",
    "balance":         "کیف پول",
    "card_to_card":    "کارت به کارت",
    "manual_crypto":   "کریپتو دستی",
    "tetrapay":        "تتراپی",
    "nowpayments":     "ارز دیجیتال",
    "tronado":         "ترونادو",
    "crypto":          "ارز دیجیتال",
    "autoconfirm":     "تأیید خودکار کریپتو",
}


def _method_fa(method: str | None) -> str:
    if not method:
        return "—"
    return _METHOD_LABELS_FA.get(method.lower(), method)


# ── Bytes-to-GB / day formatters ───────────────────────────────────────


def _gb_label(volume_bytes: int) -> str:
    if not volume_bytes or volume_bytes <= 0:
        return "—"
    gb = volume_bytes / (1024**3)
    if gb >= 10:
        return f"{int(round(gb))} گیگ"
    return f"{gb:.1f} گیگ"


def _days_label(starts_at, ends_at) -> str:
    if not ends_at:
        return "—"
    if starts_at:
        delta = ends_at - starts_at
    else:
        delta = ends_at - datetime.now(timezone.utc)
    days = max(int(delta.total_seconds() // 86400), 0)
    return f"{days} روز"


# ── User & server lookups (cheap, on-demand) ────────────────────────────


def _esc(value) -> str:
    """Telegram HTML escape — drop everything to a safe span."""
    return html.escape("" if value is None else str(value), quote=False)


def _user_link(user: User) -> str:
    """Inline mention of `user`. Uses @username when present, else a
    Telegram `tg://user?id=` link (rendered as their first name)."""
    if user.username:
        return f"@{_esc(user.username)}"
    label = _esc(user.first_name or str(user.telegram_id))
    return f"<a href='tg://user?id={user.telegram_id}'>{label}</a>"


async def _user_wallet_balance_label(session: AsyncSession, user: User) -> str:
    """Return the user's current wallet balance in the operator-configured
    display currency (USD or Toman). Falls back to '—' on any error."""
    try:
        repo = AppSettingsRepository(session)
        rate = await repo.get_toman_rate()
        mode = await repo.get_display_currency()
    except Exception:
        rate, mode = 100000, "USD"
    try:
        balance = user.wallet.balance if (user.wallet and user.wallet.balance is not None) else Decimal("0")
    except Exception:
        return "—"
    return format_money(balance, mode=mode, toman_rate=rate)


async def _amount_label(session: AsyncSession, usd_amount: Decimal | float) -> str:
    """Same display logic but for a one-off payment amount."""
    try:
        repo = AppSettingsRepository(session)
        rate = await repo.get_toman_rate()
        mode = await repo.get_display_currency()
    except Exception:
        rate, mode = 100000, "USD"
    return format_money(usd_amount, mode=mode, toman_rate=rate)


def _phone_label(user: User) -> str | None:
    """Best-effort verified-phone lookup."""
    try:
        from services.phone_verification import get_verified_phone
        return get_verified_phone(user)
    except Exception:
        return None


async def _server_label_async(session: AsyncSession, sub: Subscription) -> str | None:
    """Human-friendly server name from the subscription's X-UI client.

    Does its OWN eager-loaded fetch so a caller who passes a Subscription
    without `xui_client.inbound.server` chain pre-loaded doesn't trip a
    MissingGreenlet inside this notification path.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from models.xui import XUIClientRecord, XUIInboundRecord
    try:
        client = await session.scalar(
            select(XUIClientRecord)
            .options(
                selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server),
            )
            .where(XUIClientRecord.subscription_id == sub.id)
        )
        if client and client.inbound and client.inbound.server:
            return client.inbound.server.name or None
    except Exception:
        return None
    return None


def _config_name_for_sub(sub: Subscription, fallback: str | None = None) -> str:
    """Display name to use for a subscription — same logic as the bot
    My-Configs view: legacy_remark for imports, else xui_client.username,
    else plan name, else 'سرویس'."""
    try:
        if getattr(sub, "source", None) == "imported_legacy" and getattr(sub, "legacy_remark", None):
            return sub.legacy_remark
        if sub.xui_client and sub.xui_client.username:
            return sub.xui_client.username
    except Exception:
        pass
    if sub.plan and sub.plan.name:
        return sub.plan.name
    return fallback or "سرویس"


# ── Section builders ────────────────────────────────────────────────────


def _user_section(user: User, wallet_label: str) -> str:
    phone = _phone_label(user)
    parts: list[str] = []
    parts.append("💬 <b>مشخصات کاربر</b>")
    parts.append(f"🪪 آیدی کاربر: <code>{_esc(user.telegram_id)}</code>")
    parts.append(f"👤 اسم کاربر: {_esc(user.first_name or '—')}")
    if user.username:
        parts.append(f"💬 نام کاربری: @{_esc(user.username)}")
    else:
        parts.append("💬 نام کاربری: —")
    if phone:
        parts.append(f"⚡ شماره تماس: <code>{_esc(phone)}</code>")
    parts.append(f"💰 موجودی کاربر: <b>{wallet_label}</b>")
    return "\n".join(parts)


def _service_section(
    *,
    server: str | None,
    config_name: str,
    volume_bytes: int,
    days_label: str,
    amount_label: str,
    amount_kind: str = "پرداختی",   # "پرداختی" for purchases, "افزایش" for top-ups, etc.
) -> str:
    parts: list[str] = []
    parts.append("💬 <b>مشخصات سرویس</b>")
    parts.append(f"🚦 سرور: {_esc(server or '—')}")
    parts.append(f"📌 نام سرویس: <code>{_esc(config_name)}</code>")
    if volume_bytes:
        parts.append(f"💾 حجم سرویس: {_gb_label(volume_bytes)}")
    if days_label and days_label != "—":
        parts.append(f"⏰ مدت سرویس: {_esc(days_label)}")
    parts.append(f"💵 مبلغ {amount_kind}: <b>{amount_label}</b>")
    return "\n".join(parts)


def _wrap_event(
    *,
    header: str,
    method_fa: str,
    sections: Iterable[str],
    footer: str,
    when_dt: datetime | None = None,
) -> str:
    """Glue header + sections + Jalali timestamp + footer."""
    when_dt = when_dt or datetime.now(timezone.utc)
    body_parts: list[str] = []
    body_parts.append(f"<b>{header}</b> ( {method_fa} )")
    body_parts.append(_DIVIDER)
    body_parts.append("")
    body_parts.append("\n\n".join(sections))
    body_parts.append("")
    body_parts.append(f"<i>{format_jalali(when_dt)}</i>")
    body_parts.append("")
    body_parts.append(footer)
    return "\n".join(body_parts)


# ── Public helpers ──────────────────────────────────────────────────────


async def notify_purchase(
    session: AsyncSession,
    bot: Bot,
    *,
    user: User,
    subscription: Subscription,
    price_usd: Decimal | float,
    payment_method: str,
    config_name: str | None = None,
) -> None:
    """Polished sales-report on a successful new-config purchase."""
    method_fa = _method_fa(payment_method)
    wallet_label = await _user_wallet_balance_label(session, user)
    amount_label = await _amount_label(session, price_usd)

    sections = [
        _user_section(user, wallet_label),
        _service_section(
            server=await _server_label_async(session, subscription),
            config_name=config_name or _config_name_for_sub(subscription),
            volume_bytes=int(subscription.volume_bytes or 0),
            days_label=_days_label(subscription.starts_at, subscription.ends_at),
            amount_label=amount_label,
            amount_kind="پرداختی",
        ),
    ]
    text = _wrap_event(
        header="🛒 | خرید جدید",
        method_fa=method_fa,
        sections=sections,
        footer="بنازم خرید جدید ❤️",
        when_dt=getattr(subscription, "created_at", None) or datetime.now(timezone.utc),
    )
    try:
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_purchase failed: %s", exc)


async def notify_renewal(
    session: AsyncSession,
    bot: Bot,
    *,
    user: User,
    subscription: Subscription,
    renew_type: str,         # "volume" | "time"
    amount: float,            # gigabytes for volume, days for time
    price_usd: Decimal | float,
    payment_method: str,
) -> None:
    method_fa = _method_fa(payment_method)
    wallet_label = await _user_wallet_balance_label(session, user)
    amount_label = await _amount_label(session, price_usd)

    if renew_type == "volume":
        header = "💸 | افزایش حجم با"
        volume_for_display = int(amount * 1024**3)
        days_for_display = "—"
        amount_kind = "افزایش"
    else:
        header = "⏳ | افزایش زمان با"
        volume_for_display = 0
        days_for_display = f"{int(amount)} روز"
        amount_kind = "افزایش"

    sections = [
        _user_section(user, wallet_label),
        _service_section(
            server=await _server_label_async(session, subscription),
            config_name=_config_name_for_sub(subscription),
            volume_bytes=volume_for_display,
            days_label=days_for_display,
            amount_label=amount_label,
            amount_kind="پرداختی",
        ),
    ]
    # Renewal: also surface the renewal-specific amount line.
    addendum = (
        f"\n\n📈 نوع تمدید: {('حجم' if renew_type == 'volume' else 'زمان')}\n"
        f"➕ مقدار افزوده: <b>{_esc(amount)}</b> "
        f"{('گیگ' if renew_type == 'volume' else 'روز')}"
    )
    sections[1] = sections[1] + addendum  # tack onto the service section

    text = _wrap_event(
        header=header,
        method_fa=method_fa,
        sections=sections,
        footer="بنازم تمدید جدید 🔄",
    )
    try:
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_renewal failed: %s", exc)


async def notify_wallet_topup(
    session: AsyncSession,
    bot: Bot,
    *,
    user: User,
    amount_usd: Decimal | float,
    payment_method: str,
    tx_hash: str | None = None,
) -> None:
    """Top-up reports (manual crypto / TetraPay / card-to-card / autoconfirm)."""
    method_fa = _method_fa(payment_method)
    wallet_label = await _user_wallet_balance_label(session, user)
    amount_label = await _amount_label(session, amount_usd)

    user_section = _user_section(user, wallet_label)
    txn_lines = [
        "💬 <b>مشخصات تراکنش</b>",
        f"💵 مبلغ شارژ: <b>{amount_label}</b>",
        f"💳 روش: {_esc(method_fa)}",
    ]
    if tx_hash:
        txn_lines.append(f"🔗 TX: <code>{_esc(str(tx_hash))}</code>")

    text = _wrap_event(
        header="💰 | شارژ کیف پول",
        method_fa=method_fa,
        sections=[user_section, "\n".join(txn_lines)],
        footer="بنازم شارژ جدید 💵",
    )
    try:
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_wallet_topup failed: %s", exc)
