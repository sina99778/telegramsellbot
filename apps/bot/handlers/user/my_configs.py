from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.formatting import format_volume_bytes
from core.qr import make_qr_bytes
from core.texts import Buttons
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord
from repositories.user import UserRepository
from services.xui.runtime import build_vless_uri, ensure_inbound_server_loaded


logger = logging.getLogger(__name__)

router = Router(name="user-my-configs")

_ACTIVE_STATUSES = {"pending_activation", "active"}


@router.message(F.text == Buttons.MY_CONFIGS)
async def my_configs_handler(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("حساب شما پیدا نشد. لطفاً /start را بزنید.")
        return

    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
        )
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(list(_ACTIVE_STATUSES)),
        )
        .order_by(Subscription.created_at.desc())
    )
    subscriptions = list(result.scalars().all())

    if not subscriptions:
        await message.answer(
            "📭 *شما هیچ کانفیگ فعالی ندارید\.*\n\n"
            "از دکمه *خرید کانفیگ* می\u200cتوانید یک پلن تهیه کنید\.",
            parse_mode="MarkdownV2",
        )
        return

    await message.answer(
        f"📋 *کانفیگ‌های فعال شما* \\({len(subscriptions)} عدد\\):",
        parse_mode="MarkdownV2",
    )

    for idx, sub in enumerate(subscriptions, start=1):
        plan = sub.plan
        xui = sub.xui_client

        plan_name = _escape(plan.name if plan else "نامشخص")
        volume_total = format_volume_bytes(sub.volume_bytes)
        volume_used = format_volume_bytes(sub.used_bytes)
        volume_remaining = format_volume_bytes(max(sub.volume_bytes - sub.used_bytes, 0))

        # Time remaining
        if sub.ends_at is not None:
            now = datetime.now(timezone.utc)
            remaining_days = max((sub.ends_at - now).days, 0)
            ends_label = f"{remaining_days} روز مانده"
        elif sub.status == "pending_activation":
            ends_label = "هنوز فعال نشده"
        else:
            ends_label = "نامحدود"

        sub_link = sub.sub_link or (xui.sub_link if xui else None) or "-"

        # Try to reconstruct vless URI from xui record
        vless_uri = None
        if xui and xui.inbound:
            try:
                inbound = xui.inbound
                # Load server via inbound relation (already loaded via selectinload)
                if inbound.server:
                    # Extract sub_id safely from sub_link
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
                        remark=plan.name if plan else "VPN",
                    )
            except Exception as exc:
                logger.warning("Failed to build vless_uri for sub %s: %s", sub.id, exc)

        # Build card text
        text = (
            f"*کانفیگ {idx}* — {plan_name}\n\n"
            f"💾 حجم کل: `{_escape(volume_total)}`\n"
            f"📊 مصرف شده: `{_escape(volume_used)}`\n"
            f"✅ باقی‌مانده: `{_escape(volume_remaining)}`\n"
            f"📅 زمان: {_escape(ends_label)}\n"
            f"🔄 وضعیت: {_escape(_status_fa(sub.status))}\n\n"
            f"🔗 *ساب لینک:*\n`{_escape(sub_link)}`\n"
        )

        if vless_uri:
            text += f"\n📋 *کانفیگ مستقیم:*\n`{_escape(vless_uri)}`\n"

        await message.answer(text, parse_mode="MarkdownV2")

        # Send QR only if we have a VLESS URI
        if vless_uri:
            qr_bytes = make_qr_bytes(vless_uri)
            if qr_bytes:
                await bot.send_photo(
                    chat_id=message.from_user.id,
                    photo=BufferedInputFile(qr_bytes, filename=f"config_{idx}_qr.png"),
                    caption=f"📷 QR کد کانفیگ {idx}",
                )


def _escape(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def _status_fa(status: str) -> str:
    return {
        "pending_activation": "⏳ در انتظار اولین اتصال",
        "active": "✅ فعال",
        "expired": "❌ منقضی",
        "cancelled": "🚫 لغو شده",
        "refunded": "💰 استرداد شده",
    }.get(status, status)
