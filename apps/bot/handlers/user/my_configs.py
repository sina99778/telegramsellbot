from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.formatting import format_volume_bytes
from core.qr import make_qr_bytes
from core.texts import Buttons
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord
from repositories.user import UserRepository
from services.xui.runtime import build_vless_uri


logger = logging.getLogger(__name__)

router = Router(name="user-my-configs")

_ACTIVE_STATUSES = {"pending_activation", "active"}


class MyConfigCallback(CallbackData, prefix="myconfig"):
    action: str
    subscription_id: UUID


@router.message(F.text == Buttons.MY_CONFIGS)
async def my_configs_handler(message: Message, session: AsyncSession) -> None:
    """Show a list of inline buttons for each active config."""
    if message.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("حساب شما پیدا نشد. لطفاً /start را بزنید.")
        return

    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(list(_ACTIVE_STATUSES)),
        )
        .order_by(Subscription.created_at.desc())
    )
    subscriptions = list(result.scalars().all())

    if not subscriptions:
        await message.answer(
            "📭 شما هیچ کانفیگ فعالی ندارید.\n\n"
            "از دکمه «خرید کانفیگ» می‌توانید یک پلن تهیه کنید."
        )
        return

    builder = InlineKeyboardBuilder()
    for idx, sub in enumerate(subscriptions, start=1):
        plan_name = sub.plan.name if sub.plan else "نامشخص"
        status_emoji = "✅" if sub.status == "active" else "⏳"
        label = f"{status_emoji} {plan_name}"

        # Add remaining time/volume hint
        if sub.ends_at is not None:
            now = datetime.now(timezone.utc)
            remaining_days = max((sub.ends_at - now).days, 0)
            label += f" — {remaining_days} روز"
        elif sub.status == "pending_activation":
            label += " — هنوز فعال نشده"

        builder.button(
            text=label,
            callback_data=MyConfigCallback(
                action="view",
                subscription_id=sub.id,
            ).pack(),
        )
    builder.adjust(1)

    await message.answer(
        f"📋 کانفیگ‌های فعال شما ({len(subscriptions)} عدد):\n"
        "برای مشاهده جزئیات روی هر کدام بزنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "myconfig:back_to_list")
async def my_configs_back_to_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Re-render the config list when user presses back."""
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    result = await session.execute(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(list(_ACTIVE_STATUSES)),
        )
        .order_by(Subscription.created_at.desc())
    )
    subscriptions = list(result.scalars().all())

    if not subscriptions:
        if callback.message is not None:
            await callback.message.answer("📭 شما هیچ کانفیگ فعالی ندارید.")
        return

    builder = InlineKeyboardBuilder()
    for sub in subscriptions:
        plan_name = sub.plan.name if sub.plan else "نامشخص"
        status_emoji = "✅" if sub.status == "active" else "⏳"
        label = f"{status_emoji} {plan_name}"
        if sub.ends_at is not None:
            now = datetime.now(timezone.utc)
            remaining_days = max((sub.ends_at - now).days, 0)
            label += f" — {remaining_days} روز"
        elif sub.status == "pending_activation":
            label += " — هنوز فعال نشده"
        builder.button(
            text=label,
            callback_data=MyConfigCallback(action="view", subscription_id=sub.id).pack(),
        )
    builder.adjust(1)

    if callback.message is not None:
        await callback.message.answer(
            f"📋 کانفیگ‌های فعال شما ({len(subscriptions)} عدد):\n"
            "برای مشاهده جزئیات روی هر کدام بزنید:",
            reply_markup=builder.as_markup(),
        )


@router.callback_query(MyConfigCallback.filter(F.action == "view"))
async def my_config_detail_handler(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Show full details for a single config when user clicks its button."""
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        if callback.message is not None:
            await callback.message.answer("حساب شما پیدا نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None:
        if callback.message is not None:
            await callback.message.answer("کانفیگ پیدا نشد یا متعلق به شما نیست.")
        return

    plan = sub.plan
    xui = sub.xui_client

    plan_name = plan.name if plan else "نامشخص"
    volume_total = format_volume_bytes(sub.volume_bytes)
    volume_used = format_volume_bytes(sub.used_bytes)
    volume_remaining = format_volume_bytes(max(sub.volume_bytes - sub.used_bytes, 0))

    # Time remaining
    if sub.ends_at is not None:
        now = datetime.now(timezone.utc)
        remaining_days = max((sub.ends_at - now).days, 0)
        ends_label = f"{remaining_days} روز مانده"
    elif sub.status == "pending_activation":
        ends_label = "هنوز فعال نشده (از اولین اتصال شروع می‌شود)"
    else:
        ends_label = "نامحدود"

    sub_link = sub.sub_link or (xui.sub_link if xui else None) or "-"

    # Try to build vless URI from xui record
    vless_uri = None
    if xui and xui.inbound:
        try:
            inbound = xui.inbound
            if inbound.server:
                raw_sub_link = sub_link
                if raw_sub_link and raw_sub_link != "-" and "/" in raw_sub_link:
                    extracted_sub_id = raw_sub_link.rsplit("/", 1)[-1]
                else:
                    extracted_sub_id = ""
                vless_uri = build_vless_uri(
                    client_uuid=xui.client_uuid,
                    server=inbound.server,
                    inbound=inbound,
                    sub_id=extracted_sub_id,
                    remark=plan_name,
                )
        except Exception as exc:
            logger.warning("Failed to build vless_uri for sub %s: %s", sub.id, exc)

    # Build message with MarkdownV2
    lines = [
        f"📛 *نام کانفیگ*: `{_escape(xui.username if xui else '-')}`",
        f"📦 *پلن*: `{_escape(plan_name)}`",
        f"💾 *حجم کل*: `{_escape(volume_total)}`",
        f"📊 *مصرف شده*: `{_escape(volume_used)}`",
        f"✅ *باقی‌مانده*: `{_escape(volume_remaining)}`",
        f"📅 *زمان*: `{_escape(ends_label)}`",
        f"🔄 *وضعیت*: `{_escape(_status_fa(sub.status))}`",
        "",
        "🔗 *ساب لینک \\(برای وارد کردن در اپ\\)*:",
        f"`{_escape(sub_link)}`",
    ]
    if vless_uri:
        lines.append("")
        lines.append("📋 *لینک کانفیگ مستقیم*:")
        lines.append(f"`{_escape(vless_uri)}`")

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if sub.status in ("active", "pending_activation"):
        builder.button(text=Buttons.RENEW_SERVICE, callback_data=MyConfigCallback(action="renew", subscription_id=sub.id).pack())
    builder.button(text=Buttons.BACK, callback_data="myconfig:back_to_list")
    builder.adjust(1)

    # If QR code is available, send photo with text as caption
    if vless_uri:
        qr_bytes = make_qr_bytes(vless_uri)
        if qr_bytes:
            # Delete the previous text-only message if it was an edit from list
            # Actually, callback.message.edit_text works for text, but to switch to photo
            # we usually need to send a NEW message or use edit_message_media.
            # To keep it simple and reliable, we'll send a NEW message and try to delete the old one.
            try:
                await callback.message.delete()
            except Exception:
                pass
            
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode="MarkdownV2"
            )
            return

    # Fallback to text message if no QR or QR failed
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="MarkdownV2")
        except Exception:
            await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="MarkdownV2")


def _status_fa(status: str) -> str:
    return {
        "pending_activation": "⏳ در انتظار اولین اتصال",
        "active": "✅ فعال",
        "expired": "❌ منقضی",
        "cancelled": "🚫 لغو شده",
        "refunded": "💰 استرداد شده",
    }.get(status, status)
