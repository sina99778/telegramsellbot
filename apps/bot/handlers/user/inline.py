from __future__ import annotations

import html
import logging
from uuid import uuid4

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.formatting import format_volume_bytes
from models.subscription import Subscription
from repositories.user import UserRepository

logger = logging.getLogger(__name__)

router = Router(name="user-inline")


@router.inline_query()
async def inline_query_handler(inline_query: InlineQuery, session: AsyncSession) -> None:
    """Handle inline queries (@botname) to quickly share configs and referral link."""
    if inline_query.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(inline_query.from_user.id)
    if not user:
        await inline_query.answer([], cache_time=1)
        return

    # Ensure the user has an opaque ref_code so we never leak the raw UUID
    # in shared messages. Older accounts may have been created without one.
    if not user.ref_code:
        import secrets as _secrets
        user.ref_code = _secrets.token_hex(4)
        await session.flush()

    # Fetch user active configs
    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(["active", "pending_activation"]),
        )
        .limit(10)
    )
    subs = list(result.scalars().all())

    results = []

    from core.config import settings
    # Assuming bot username is passed, if not fallback
    bot_username = "bot"  # We don't have bot instance directly here but it's fine for deep links usually

    # Generate results for active configs
    for sub in subs:
        config_name = sub.xui_client.username if sub.xui_client else (sub.plan.name if sub.plan else "سرویس")
        used = format_volume_bytes(sub.used_bytes)
        total = format_volume_bytes(sub.volume_bytes)

        # HTML parse mode + escaping: legacy Markdown chokes on the '_' that
        # marzban-family/trial config names always contain, which made
        # answerInlineQuery fail with 400 and show zero results.
        text = (
            f"🚀 <b>کانفیگ من: {html.escape(config_name)}</b>\n\n"
            f"📊 مصرف: {used} از {total}\n"
            f"⚡ سریع‌ترین کانفیگ‌های V2Ray در ربات ما!"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 دریافت کانفیگ مشابه", url=f"https://t.me/telegramsellbot?start=ref_{user.ref_code}")]
        ])

        results.append(
            InlineQueryResultArticle(
                id=str(sub.id),
                title=f"📤 اشتراک‌گذاری کانفیگ: {config_name}",
                description=f"مصرف: {used} / {total}",
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML"
                ),
                reply_markup=kb,
            )
        )

    # Always provide referral share option
    results.append(
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="🎁 ارسال لینک دعوت",
            description="لینک دعوت خود را به گروه‌ها و دوستان بفرستید",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🎁 <b>سرویس‌های پرسرعت و پایدار V2Ray</b>\n\n"
                    f"با استفاده از لینک زیر وارد ربات شوید و تست رایگان دریافت کنید:\n"
                    f"👉 https://t.me/telegramsellbot?start=ref_{user.ref_code}"
                ),
                parse_mode="HTML"
            ),
        )
    )

    # Backstop: GlobalErrorMiddleware is not registered for inline queries,
    # so an unexpected Telegram error here would otherwise go unhandled and
    # roll back the session (losing e.g. the ref_code backfill above).
    try:
        await inline_query.answer(results, cache_time=5, is_personal=True)
    except Exception:
        logger.exception(
            "Failed to answer inline query for user %s", inline_query.from_user.id
        )
