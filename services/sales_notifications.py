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


async def _get_user_summary(session: AsyncSession, user: User | UUID | int) -> dict:
    from sqlalchemy import select
    from models.user import User as UserModel, UserProfile
    u_id = getattr(user, "id", user)

    # Try fetching with select to avoid MissingGreenlet on expired attributes
    try:
        row = (await session.execute(
            select(UserModel.id, UserModel.telegram_id, UserModel.first_name, UserModel.username)
            .where(UserModel.id == u_id)
        )).first()
        if row:
            uid, tg_id, fname, uname = row
            phone = None
            try:
                prof_notes = await session.scalar(
                    select(UserProfile.notes).where(UserProfile.user_id == uid)
                )
                if prof_notes and isinstance(prof_notes, str) and prof_notes.startswith("{"):
                    import json
                    pdict = json.loads(prof_notes)
                    pmeta = pdict.get("verified_phone")
                    if isinstance(pmeta, dict) and pmeta.get("phone"):
                        phone = pmeta.get("phone")
            except Exception:
                pass
            return {
                "id": uid,
                "telegram_id": tg_id,
                "first_name": fname,
                "username": uname,
                "phone": phone,
            }
    except Exception:
        pass

    # Fallback to direct attribute access if user object was provided
    if isinstance(user, User):
        try:
            return {
                "id": getattr(user, "id", None),
                "telegram_id": getattr(user, "telegram_id", 0),
                "first_name": getattr(user, "first_name", "—"),
                "username": getattr(user, "username", None),
                "phone": _phone_label(user),
            }
        except Exception:
            pass

    return {
        "id": u_id,
        "telegram_id": 0,
        "first_name": "—",
        "username": None,
        "phone": None,
    }


async def _user_wallet_balance_label(session: AsyncSession, user_id) -> str:
    """Return the user's current wallet balance in the operator-configured
    display currency (USD or Toman). Falls back to '—' on any error."""
    try:
        repo = AppSettingsRepository(session)
        rate = await repo.get_toman_rate()
        mode = await repo.get_display_currency()
    except Exception:
        rate, mode = 100000, "USD"
    if not user_id:
        return "—"
    try:
        from sqlalchemy import select
        from models.wallet import Wallet
        balance = await session.scalar(select(Wallet.balance).where(Wallet.user_id == user_id))
        if balance is None:
            balance = Decimal("0")
    except Exception:
        balance = Decimal("0")
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
    """Human-friendly server name from the subscription's X-UI client."""
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


async def _config_name_for_sub_async(session: AsyncSession, sub: Subscription, fallback: str | None = None) -> str:
    """Display name to use for a subscription."""
    try:
        from sqlalchemy import select
        from models.subscription import Subscription as SubModel
        from models.xui import XUIClientRecord
        from models.plan import Plan as PlanModel

        row = (await session.execute(
            select(SubModel.legacy_remark, SubModel.source, SubModel.plan_id)
            .where(SubModel.id == sub.id)
        )).first()

        if row:
            legacy_remark, source, plan_id = row
            if source == "imported_legacy" and legacy_remark:
                return legacy_remark
            
            client_uname = await session.scalar(
                select(XUIClientRecord.username).where(XUIClientRecord.subscription_id == sub.id)
            )
            if client_uname:
                return client_uname

            if plan_id:
                plan_name = await session.scalar(
                    select(PlanModel.name).where(PlanModel.id == plan_id)
                )
                if plan_name:
                    return plan_name
    except Exception:
        pass
    return fallback or "سرویس"


# ── Section builders ────────────────────────────────────────────────────


def _user_section(user_summary: dict, wallet_label: str) -> str:
    parts: list[str] = []
    parts.append("💬 <b>مشخصات کاربر</b>")
    parts.append(f"🪪 آیدی کاربر: <code>{_esc(user_summary.get('telegram_id'))}</code>")
    parts.append(f"👤 اسم کاربر: {_esc(user_summary.get('first_name') or '—')}")
    uname = user_summary.get("username")
    if uname:
        parts.append(f"💬 نام کاربری: @{_esc(uname)}")
    else:
        parts.append("💬 نام کاربری: —")
    phone = user_summary.get("phone")
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
    try:
        user_summary = await _get_user_summary(session, user)
        method_fa = _method_fa(payment_method)
        wallet_label = await _user_wallet_balance_label(session, user_summary.get("id"))
        amount_label = await _amount_label(session, price_usd)

        cfg_name = config_name or await _config_name_for_sub_async(session, subscription)
        server_lbl = await _server_label_async(session, subscription)
        volume_bytes = int(getattr(subscription, "volume_bytes", 0) or 0)
        days_label = _days_label(getattr(subscription, "starts_at", None), getattr(subscription, "ends_at", None))

        sections = [
            _user_section(user_summary, wallet_label),
            _service_section(
                server=server_lbl,
                config_name=cfg_name,
                volume_bytes=volume_bytes,
                days_label=days_label,
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
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_purchase failed: %s", exc, exc_info=True)


async def notify_renewal(
    session: AsyncSession,
    bot: Bot,
    *,
    user: User,
    subscription: Subscription,
    renew_type: str,         # "volume" | "time" | "plan"
    amount: float,            # gigabytes for volume, days for time
    price_usd: Decimal | float,
    payment_method: str,
) -> None:
    try:
        user_summary = await _get_user_summary(session, user)
        method_fa = _method_fa(payment_method)
        wallet_label = await _user_wallet_balance_label(session, user_summary.get("id"))
        amount_label = await _amount_label(session, price_usd)

        cfg_name = await _config_name_for_sub_async(session, subscription)
        server_lbl = await _server_label_async(session, subscription)

        if renew_type == "volume":
            header = "💸 | افزایش حجم با"
            volume_for_display = int(amount * 1024**3)
            days_for_display = "—"
            amount_kind = "افزایش"
        elif renew_type == "plan":
            # Plan renewal resets quota AND days to the plan's fresh values — by
            # the time this notification fires the subscription columns already
            # hold the post-reset numbers, so show those.
            header = "🔄 | تمدید پلن فعلی با"
            volume_for_display = int(getattr(subscription, "volume_bytes", 0) or 0)
            days_for_display = "بازنشانی کامل"
            amount_kind = "تمدید"
        else:
            header = "⏳ | افزایش زمان با"
            volume_for_display = 0
            days_for_display = f"{int(amount)} روز"
            amount_kind = "افزایش"

        sections = [
            _user_section(user_summary, wallet_label),
            _service_section(
                server=server_lbl,
                config_name=cfg_name,
                volume_bytes=volume_for_display,
                days_label=days_for_display,
                amount_label=amount_label,
                amount_kind="پرداختی",
            ),
        ]
        # Renewal: also surface the renewal-specific amount line.
        if renew_type == "plan":
            addendum = (
                "\n\n📈 نوع تمدید: پلن فعلی\n"
                "🔄 حجم و زمان سرویس به مقادیر جدید پلن <b>بازنشانی</b> شد"
            )
        else:
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
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_renewal failed: %s", exc, exc_info=True)


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
    try:
        user_summary = await _get_user_summary(session, user)
        method_fa = _method_fa(payment_method)
        wallet_label = await _user_wallet_balance_label(session, user_summary.get("id"))
        amount_label = await _amount_label(session, amount_usd)

        user_section = _user_section(user_summary, wallet_label)
        txn_lines = [
            "💬 <b>مشخصات تراکنش</b>",
            f"💵 مبلغ شارژ: <b>{amount_label}</b>",
            f"💳 روش: {_esc(method_fa)}",
        ]
        if tx_hash and str(tx_hash) not in ("N/A", "None", ""):
            txn_lines.append(f"🔗 TX / رسید: <code>{_esc(str(tx_hash))}</code>")

        text = _wrap_event(
            header="💰 | شارژ کیف پول",
            method_fa=method_fa,
            sections=[user_section, "\n".join(txn_lines)],
            footer="بنازم شارژ جدید 💵",
        )
        await notify_sales_event(session, bot, text)
    except Exception as exc:
        logger.warning("notify_wallet_topup failed: %s", exc, exc_info=True)
