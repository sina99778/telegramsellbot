from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.utils.messaging import safe_edit_or_send
from apps.bot.states.my_configs import UserConfigSearchStates
from core.formatting import escape_markdown, format_usage_bar, format_volume_bytes
from core.texts import Buttons
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.banner import create_traffic_banner
from services.xui.runtime import build_vless_uri


logger = logging.getLogger(__name__)

router = Router(name="user-my-configs")

_ACTIVE_STATUSES = {"pending_activation", "active", "expired", "disabled"}
_CONFIGS_PAGE_SIZE = 8


class MyConfigCallback(CallbackData, prefix="myconfig"):
    action: str
    subscription_id: UUID


class MyConfigListCallback(CallbackData, prefix="myconfigs"):
    action: str
    page: int = 0


@router.message(F.text == Buttons.MY_CONFIGS)
async def my_configs_handler(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Show a list of inline buttons for each active config."""
    if message.from_user is None:
        return
    # Clear any ongoing state (e.g. search)
    await state.clear()

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("حساب شما پیدا نشد. لطفاً /start را بزنید.")
        return

    subscriptions, total_count, page, total_pages = await _load_user_config_page(
        session=session,
        user_id=user.id,
        page=0,
    )

    if not subscriptions:
        await message.answer(
            "📭 شما هیچ کانفیگ فعالی ندارید.\n\n"
            "از دکمه «خرید کانفیگ» می‌توانید یک پلن تهیه کنید."
        )
        return

    await message.answer(
        _build_config_list_text(total_count, page, total_pages),
        reply_markup=_build_config_list_keyboard(subscriptions, page, total_pages),
    )


@router.callback_query(F.data == "user:my_configs")
@router.callback_query(F.data == "myconfig:back_to_list")
async def my_configs_back_to_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Re-render the config list when user presses back."""
    await callback.answer()
    await _render_config_list_callback(callback=callback, session=session, page=0)


@router.callback_query(MyConfigListCallback.filter(F.action == "page"))
async def my_configs_page_handler(
    callback: CallbackQuery,
    callback_data: MyConfigListCallback,
    session: AsyncSession,
) -> None:
    """Switch between pages in the user's config list."""
    await callback.answer()
    await _render_config_list_callback(
        callback=callback,
        session=session,
        page=callback_data.page,
    )


@router.callback_query(MyConfigListCallback.filter(F.action == "noop"))
async def my_configs_noop_handler(callback: CallbackQuery) -> None:
    await callback.answer()


async def _render_config_list_callback(
    *,
    callback: CallbackQuery,
    session: AsyncSession,
    page: int,
) -> None:
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    subscriptions, total_count, page, total_pages = await _load_user_config_page(
        session=session,
        user_id=user.id,
        page=page,
    )

    if not subscriptions:
        await safe_edit_or_send(callback, "📭 شما هیچ کانفیگ فعالی ندارید.")
        return

    text = _build_config_list_text(total_count, page, total_pages)
    reply_markup = _build_config_list_keyboard(subscriptions, page, total_pages)

    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            await safe_edit_or_send(callback, text, reply_markup=reply_markup)


async def _load_user_config_page(
    *,
    session: AsyncSession,
    user_id: UUID,
    page: int,
) -> tuple[list[Subscription], int, int, int]:
    status_filter = Subscription.status.in_(list(_ACTIVE_STATUSES))
    total_count = await session.scalar(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == user_id,
            status_filter,
        )
    )
    total_count = total_count or 0
    total_pages = max((total_count + _CONFIGS_PAGE_SIZE - 1) // _CONFIGS_PAGE_SIZE, 1)
    page = max(0, min(page, total_pages - 1))

    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.user_id == user_id,
            status_filter,
        )
        .order_by(Subscription.created_at.desc())
        .limit(_CONFIGS_PAGE_SIZE)
        .offset(page * _CONFIGS_PAGE_SIZE)
    )
    return list(result.scalars().all()), total_count, page, total_pages


def _build_config_list_keyboard(
    subscriptions: list[Subscription],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subscriptions:
        builder.button(
            text=_build_config_button_label(sub),
            callback_data=MyConfigCallback(
                action=f"viewp{page}",
                subscription_id=sub.id,
            ).pack(),
        )
    builder.adjust(1)

    if total_pages > 1:
        nav_buttons: list[InlineKeyboardButton] = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="⬅️ قبلی",
                    callback_data=MyConfigListCallback(action="page", page=page - 1).pack(),
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data=MyConfigListCallback(action="noop", page=page).pack(),
            )
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="بعدی ➡️",
                    callback_data=MyConfigListCallback(action="page", page=page + 1).pack(),
                )
            )
        builder.row(*nav_buttons)

    # Search button
    builder.row(
        InlineKeyboardButton(
            text="🔍 جستجوی کانفیگ",
            callback_data=MyConfigListCallback(action="search", page=0).pack(),
        )
    )

    return builder.as_markup()


def _build_config_button_label(sub: Subscription) -> str:
    config_name = (
        sub.xui_client.username if sub.xui_client else (sub.plan.name if sub.plan else "نامشخص")
    )
    status_emoji = {"active": "✅", "pending_activation": "⏳", "expired": "❌", "disabled": "🚫"}.get(sub.status, "❓")
    label = f"{status_emoji} {config_name}"
    if sub.ends_at is not None:
        now = datetime.now(timezone.utc)
        remaining_days = max((sub.ends_at - now).days, 0)
        label += f" — {remaining_days} روز"
    elif sub.status == "pending_activation":
        label += " — هنوز فعال نشده"
    elif sub.status == "disabled":
        label += " — غیرفعال"
    return label


def _build_config_list_text(total_count: int, page: int, total_pages: int) -> str:
    page_hint = f"\nصفحه {page + 1} از {total_pages}" if total_pages > 1 else ""
    return (
        f"📋 کانفیگ‌های شما ({total_count} عدد):{page_hint}\n"
        "برای مشاهده جزئیات روی هر کدام بزنید:"
    )


def _config_list_page_from_action(action: str) -> int:
    if not action.startswith("viewp"):
        return 0
    try:
        return max(int(action.removeprefix("viewp")), 0)
    except ValueError:
        return 0


# ─── Config Search ────────────────────────────────────────────────────────────


@router.callback_query(MyConfigListCallback.filter(F.action == "search"))
async def my_configs_search_prompt(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """User clicked the search button — ask for a config name."""
    await callback.answer()
    await state.set_state(UserConfigSearchStates.waiting_for_search_query)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ انصراف", callback_data=MyConfigListCallback(action="page", page=0).pack())
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "🔍 نام کانفیگ مورد نظر را تایپ کنید:\n"
        "(دقیق یا بخشی از نام کانفیگ)",
        reply_markup=builder.as_markup(),
    )


@router.message(UserConfigSearchStates.waiting_for_search_query)
async def my_configs_search_handler(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Handle the search query typed by user."""
    if message.from_user is None or not message.text:
        return

    query = message.text.strip().lower()
    if not query:
        await message.answer("❌ متن جستجو خالی است.")
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await state.clear()
        return

    try:
        # Search in XUIClientRecord.username and plan.name
        from models.xui import XUIClientRecord
        from models.plan import Plan
        from sqlalchemy import or_, func as sqfunc

        result = await session.execute(
            select(Subscription)
            .join(XUIClientRecord, XUIClientRecord.subscription_id == Subscription.id, isouter=True)
            .join(Plan, Subscription.plan_id == Plan.id, isouter=True)
            .options(
                selectinload(Subscription.plan),
                selectinload(Subscription.xui_client),
            )
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(list(_ACTIVE_STATUSES)),
                or_(
                    sqfunc.lower(XUIClientRecord.username).contains(query),
                    sqfunc.lower(Plan.name).contains(query),
                ),
            )
            .distinct()
            .order_by(Subscription.created_at.desc())
            .limit(20)
        )
        subs = list(result.scalars().all())
    except Exception as exc:
        logger.error("Config search failed: %s", exc, exc_info=True)
        await state.clear()
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 بازگشت", callback_data=MyConfigListCallback(action="page", page=0).pack())
        builder.adjust(1)
        await message.answer(
            "❌ خطا در جستجو. لطفاً دوباره تلاش کنید.",
            reply_markup=builder.as_markup(),
        )
        return

    await state.clear()

    if not subs:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 بازگشت", callback_data=MyConfigListCallback(action="page", page=0).pack())
        builder.adjust(1)
        await message.answer(
            f"🔍 هیچ کانفیگی با '‏{query}‏' یافت نشد.",
            reply_markup=builder.as_markup(),
        )
        return

    builder = InlineKeyboardBuilder()
    for sub in subs:
        builder.button(
            text=_build_config_button_label(sub),
            callback_data=MyConfigCallback(action="view", subscription_id=sub.id).pack(),
        )
    builder.button(text="🔄 بازگشت به لیست", callback_data=MyConfigListCallback(action="page", page=0).pack())
    builder.adjust(1)

    await message.answer(
        f"🔍 نتایج جستجو برای '‏{query}‏' ({len(subs)} کانفیگ):",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(MyConfigCallback.filter(F.action.startswith("view")))
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
        await safe_edit_or_send(callback, "حساب شما پیدا نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
        .with_for_update()
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد یا متعلق به شما نیست.")
        return

    plan = sub.plan
    xui = sub.xui_client

    plan_name = plan.name if plan else "نامشخص"
    config_name = xui.username if xui else plan_name

    # ── Fetch real-time usage from X-UI panel ──
    realtime_ok = False
    realtime_error = ""
    try:
        from apps.worker.jobs.subscriptions import get_realtime_usage
        usage = await get_realtime_usage(session, sub)
        if usage is not None:
            realtime_ok = True
    except Exception as exc:
        logger.error("Failed to fetch realtime usage for sub %s: %s", sub.id, exc, exc_info=True)
        realtime_error = str(exc)[:100]

    volume_total = format_volume_bytes(sub.volume_bytes)
    volume_used = format_volume_bytes(sub.used_bytes)
    volume_remaining = format_volume_bytes(max(sub.volume_bytes - sub.used_bytes, 0))

    # Time remaining
    if sub.ends_at is not None:
        now = datetime.now(timezone.utc)
        remaining_days = max((sub.ends_at - now).days, 0)
        remaining_hours = max(int((sub.ends_at - now).total_seconds() / 3600), 0)
        if remaining_days > 0:
            ends_label = f"{remaining_days} روز مانده"
        else:
            ends_label = f"{remaining_hours} ساعت مانده"
    elif sub.status == "pending_activation":
        ends_label = "هنوز فعال نشده (از اولین اتصال شروع می‌شود)"
    else:
        ends_label = "نامحدود"

    sub_link = sub.sub_link or (xui.sub_link if xui else None) or "-"

    # Dynamically rebuild sub_link from current server settings
    # This ensures users always see the correct link even if admin changed server config
    if xui and xui.inbound and xui.inbound.server and sub_link != "-" and "/" in sub_link:
        try:
            from services.xui.runtime import build_sub_link
            stored_sub_id = sub_link.rsplit("/", 1)[-1]
            if stored_sub_id:
                fresh_sub_link = build_sub_link(xui.inbound.server, stored_sub_id)
                if fresh_sub_link != sub_link:
                    logger.info(
                        "Sub link updated for sub %s: %s -> %s",
                        sub.id, sub_link, fresh_sub_link,
                    )
                    sub.sub_link = fresh_sub_link
                    xui.sub_link = fresh_sub_link
                    sub_link = fresh_sub_link
        except Exception as exc:
            logger.warning("Failed to rebuild sub_link for sub %s: %s", sub.id, exc)

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
                    remark=config_name,
                )
        except Exception as exc:
            logger.warning("Failed to build vless_uri for sub %s: %s", sub.id, exc)
    else:
        from models.ready_config import ReadyConfigItem
        ready_item = await session.scalar(select(ReadyConfigItem).where(ReadyConfigItem.subscription_id == sub.id))
        if ready_item:
            vless_uri = ready_item.content.split("|")[0].strip()

    # Build message with HTML
    import html
    esc = html.escape
    usage_bar = format_usage_bar(sub.used_bytes, sub.volume_bytes)
    if realtime_ok:
        sync_label = "✅ لحظه‌ای"
    elif realtime_error:
        sync_label = f"❌ خطا: {esc(realtime_error)}"
    else:
        sync_label = "⚠️ آفلاین"
    lines = [
        f"📛 <b>نام کانفیگ</b>: <code>{esc(xui.username if xui else '-')}</code>",
        f"📦 <b>پلن</b>: <code>{esc(plan_name)}</code>",
        f"💾 <b>حجم کل</b>: <code>{esc(volume_total)}</code>",
        f"📊 <b>مصرف شده</b>: <code>{esc(volume_used)}</code>",
        f"✅ <b>باقی‌مانده</b>: <code>{esc(volume_remaining)}</code>",
        f"📶 <b>مصرف</b>: <code>{esc(usage_bar)}</code>",
        f"📅 <b>زمان</b>: <code>{esc(ends_label)}</code>",
        f"🔄 <b>وضعیت</b>: <code>{esc(_status_fa(sub.status))}</code>",
        f"📡 <b>سینک</b>: {sync_label}",
        "",
        "🔗 <b>ساب لینک (برای وارد کردن در اپ)</b>:",
        f"<code>{esc(sub_link)}</code>",
    ]
    if vless_uri:
        lines.append("")
        lines.append("📋 <b>لینک کانفیگ مستقیم</b>:")
        lines.append(f"<code>{esc(vless_uri)}</code>")

    text = "\n".join(lines)

    # Check if server is deleted or missing
    server_deleted = False
    if xui and xui.inbound:
        server = xui.inbound.server
        if server is None or server.health_status == "deleted" or not server.is_active:
            server_deleted = True
    elif xui and xui.inbound is None:
        server_deleted = True

    builder = InlineKeyboardBuilder()
    if sub.status in ("active", "pending_activation") and not server_deleted:
        builder.button(text="📊 بروزرسانی حجم", callback_data=MyConfigCallback(action="refresh_usage", subscription_id=sub.id).pack())
        
    if sub.status in ("active", "pending_activation", "expired") and not server_deleted:
        builder.button(text=Buttons.RENEW_SERVICE, callback_data=MyConfigCallback(action="renew", subscription_id=sub.id).pack())
        
    if sub.status in ("active", "pending_activation") and not server_deleted:
        # New Feature: Change Link / Reset UUID
        builder.button(text="🔄 تغییر لینک", callback_data=MyConfigCallback(action="reset_uuid", subscription_id=sub.id).pack())
        
        # New Feature: Toggle enable status
        is_enabled = xui.is_active if xui else False
        toggle_text = "🔴 خاموش کردن" if is_enabled else "🟢 روشن کردن"
        builder.button(text=toggle_text, callback_data=MyConfigCallback(action="toggle_enable", subscription_id=sub.id).pack())
        
        # Transfer config to another user
        builder.button(text="🔀 انتقال کانفیگ", callback_data=MyConfigCallback(action="transfer", subscription_id=sub.id).pack())

    # Cancel & refund for unused configs — but NOT for ready configs (they can't be returned)
    if sub.status == "pending_activation" and sub.used_bytes == 0:
        from models.ready_config import ReadyConfigItem
        is_ready_config = await session.scalar(
            select(ReadyConfigItem.id).where(ReadyConfigItem.subscription_id == sub.id)
        )
        if not is_ready_config:
            builder.button(text="🔄 لغو و بازپرداخت", callback_data=MyConfigCallback(action="cancel_refund", subscription_id=sub.id).pack())
    # Delete: expired configs OR configs with deleted/missing server
    if sub.status == "expired" or server_deleted:
        builder.button(text="🗑 حذف کانفیگ", callback_data=MyConfigCallback(action="delete", subscription_id=sub.id).pack())
        
    if vless_uri:
        encoded_uri = urllib.parse.quote(vless_uri, safe="")
        from core.config import settings
        base = settings.web_base_url.rstrip("/")
        builder.button(text="🟢 اتصال v2rayNG", url=f"{base}/api/dl/v2rayng?url={encoded_uri}")
        builder.button(text="🍎 اتصال Shadowrocket", url=f"{base}/api/dl/shadowrocket?url={encoded_uri}")
        builder.button(text="🍎 اتصال V2Box", url=f"{base}/api/dl/v2box?url={encoded_uri}")

    back_page = _config_list_page_from_action(callback_data.action)
    builder.button(
        text=Buttons.BACK,
        callback_data=MyConfigListCallback(action="page", page=back_page).pack(),
    )
    builder.adjust(2)

    # If vless_uri is available, send photo with text as caption
    if vless_uri:
        days_left = 0
        if sub.ends_at:
            days_left = max((sub.ends_at - datetime.now(timezone.utc)).days, 0)
        
        banner_bytes = create_traffic_banner(
            config_name=config_name,
            user_id=user.id,
            status=sub.status,
            used_gb=sub.used_bytes / (1024**3),
            total_gb=sub.volume_bytes / (1024**3),
            days_left=days_left,
            is_active=(sub.status in ["active", "pending_activation"]),
            bot_username=(bot._me.username if bot._me else (await bot.get_me()).username) if bot else None,
            vless_uri=vless_uri,
        )
        if banner_bytes:
            try:
                await callback.message.delete()
            except Exception:
                pass
            
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=BufferedInputFile(banner_bytes.getvalue(), filename="banner.png"),
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            return

    # Fallback to text message
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")



def _status_fa(status: str) -> str:
    return {
        "pending_activation": "⏳ در انتظار اولین اتصال",
        "active": "✅ فعال",
        "expired": "❌ منقضی",
        "cancelled": "🚫 لغو شده",
        "refunded": "💰 استرداد شده",
    }.get(status, status)

# ─── Refresh Usage (Real-time) ────────────────────────────────────────────────


@router.callback_query(MyConfigCallback.filter(F.action == "refresh_usage"))
async def refresh_usage_handler(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
) -> None:
    """Fetch real-time volume usage from X-UI panel and show to user."""
    await callback.answer("📊 در حال بررسی...")
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
        .with_for_update()
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد.")
        return

    from apps.worker.jobs.subscriptions import get_realtime_usage
    try:
        usage = await get_realtime_usage(session, sub)
    except Exception as exc:
        logger.error("refresh_usage failed: %s", exc, exc_info=True)
        await safe_edit_or_send(callback, f"❌ خطا: {str(exc)[:200]}")
        return

    if usage is None:
        await safe_edit_or_send(callback, "❌ خطا در دریافت اطلاعات از سرور. لطفاً بعداً تلاش کنید.")
        return

    used = format_volume_bytes(usage["used_bytes"])
    total = format_volume_bytes(usage["total_bytes"])
    remaining = format_volume_bytes(usage["remaining_bytes"])
    usage_bar = format_usage_bar(usage["used_bytes"], usage["total_bytes"])

    config_name = sub.xui_client.username if sub.xui_client else "نامشخص"
    
    # Show time remaining if activated
    time_info = ""
    if sub.ends_at is not None:
        now = datetime.now(timezone.utc)
        remaining_days = max((sub.ends_at - now).days, 0)
        remaining_hours = max(int((sub.ends_at - now).total_seconds() / 3600), 0)
        if remaining_days > 0:
            time_info = f"\n📅 زمان باقی‌مانده: {remaining_days} روز"
        else:
            time_info = f"\n📅 زمان باقی‌مانده: {remaining_hours} ساعت"
    elif sub.status == "pending_activation":
        time_info = "\n📅 هنوز فعال نشده (از اولین اتصال شروع می‌شود)"
    
    status_text = _status_fa(sub.status)

    text = (
        f"📊 وضعیت لحظه‌ای کانفیگ «{config_name}»\n\n"
        f"🔄 وضعیت: {status_text}\n"
        f"💾 حجم کل: {total}\n"
        f"📤 مصرف شده: {used}\n"
        f"✅ باقی‌مانده: {remaining}\n"
        f"📶 {usage_bar}"
        f"{time_info}"
    )

    await safe_edit_or_send(callback, text)



# ─── Config Actions (Reset UUID / Toggle Enable) ─────────────────────────────


@router.callback_query(MyConfigCallback.filter(F.action == "reset_uuid"))
async def reset_uuid_handler(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
) -> None:
    """Change the UUID of a config and update the panel."""
    await callback.answer("🔄 در حال تغییر لینک...")
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
        .with_for_update()
    )
    if sub is None or sub.status not in ("active", "pending_activation"):
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد یا منقضی شده است.")
        return

    xui_record = sub.xui_client
    if not xui_record or not xui_record.inbound or not xui_record.inbound.server:
        await safe_edit_or_send(callback, "❌ سرور متصل به این کانفیگ یافت نشد.")
        return

    server_obj = xui_record.inbound.server
    if server_obj.health_status == "deleted" or not server_obj.is_active:
        await safe_edit_or_send(callback, "❌ سرور خاموش یا حذف شده است.")
        return

    import uuid as uuid_mod
    from schemas.internal.xui import XUIClient
    from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded, build_sub_link
    
    new_uuid = str(uuid_mod.uuid4())
    new_sub_id = uuid_mod.uuid4().hex[:16]  # Generate a proper new subId
    expiry_ms = int(sub.ends_at.timestamp() * 1000) if sub.ends_at else 0
    security_settings = await AppSettingsRepository(session).get_service_security_settings()

    # The URL path uses the OLD UUID to find the client
    old_client_id = xui_record.xui_client_remote_id or xui_record.client_uuid
    # The payload uses the NEW UUID as the new client identity
    updated_client = XUIClient(
        id=new_uuid,
        uuid=new_uuid,
        email=xui_record.email,
        limitIp=security_settings.xui_limit_ip,
        totalGB=sub.volume_bytes,
        expiryTime=expiry_ms,
        enable=xui_record.is_active,
        subId=new_sub_id,
        comment=xui_record.username or "",
    )

    try:
        server = ensure_inbound_server_loaded(xui_record.inbound)
        async with create_xui_client_for_server(server) as xui_client:
            await xui_client.update_client(
                inbound_id=xui_record.inbound.xui_inbound_remote_id,
                client_id=old_client_id,
                client=updated_client,
            )
        
        # Update local records
        xui_record.client_uuid = new_uuid
        xui_record.xui_client_remote_id = new_uuid
        new_sub_link = build_sub_link(server, new_sub_id)
        sub.sub_link = new_sub_link
        xui_record.sub_link = new_sub_link
            
        await session.flush()
        
        # Build new vless URI
        try:
            new_vless = build_vless_uri(
                client_uuid=new_uuid,
                server=xui_record.inbound.server,
                inbound=xui_record.inbound,
                sub_id=new_sub_id,
                remark=xui_record.username or "VPN",
            )
        except Exception:
            new_vless = None

        # Send new config info to user
        response_lines = [
            "✅ لینک کانفیگ شما با موفقیت تغییر کرد!\n",
            f"🔗 ساب لینک جدید:\n{new_sub_link}",
        ]
        if new_vless:
            response_lines.append(f"\n📋 لینک مستقیم جدید:\n{new_vless}")
        response_lines.append("\n⚠️ لینک‌های قبلی دیگر کار نمی‌کنند.")

        await safe_edit_or_send(callback, "\n".join(response_lines))
    except Exception as exc:
        logger.error("Failed to reset UUID for sub %s: %s", sub.id, exc, exc_info=True)
        error_detail = str(exc)[:150]
        await safe_edit_or_send(callback, f"❌ خطا در تغییر لینک:\n{error_detail}")


@router.callback_query(MyConfigCallback.filter(F.action == "toggle_enable"))
async def toggle_enable_handler(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
) -> None:
    """Toggle the enable status of an active config."""
    await callback.answer("⚙️ در حال تغییر وضعیت...")
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
        .with_for_update()
    )
    if sub is None or sub.status not in ("active", "pending_activation"):
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد یا منقضی شده است.")
        return

    xui_record = sub.xui_client
    if not xui_record or not xui_record.inbound or not xui_record.inbound.server:
        await safe_edit_or_send(callback, "❌ سرور متصل به این کانفیگ یافت نشد.")
        return

    server_obj = xui_record.inbound.server
    if server_obj.health_status == "deleted" or not server_obj.is_active:
        await safe_edit_or_send(callback, "❌ سرور خاموش یا حذف شده است.")
        return

    from schemas.internal.xui import XUIClient
    from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded
    
    new_enable_status = not xui_record.is_active
    expiry_ms = int(sub.ends_at.timestamp() * 1000) if sub.ends_at else 0
    security_settings = await AppSettingsRepository(session).get_service_security_settings()

    # Extract existing subId from sub_link to preserve it during update
    existing_sub_id = ""
    current_sub_link = sub.sub_link or xui_record.sub_link or ""
    if current_sub_link and "/" in current_sub_link:
        existing_sub_id = current_sub_link.rsplit("/", 1)[-1]

    updated_client = XUIClient(
        id=xui_record.xui_client_remote_id or xui_record.client_uuid,
        uuid=xui_record.client_uuid,
        email=xui_record.email,
        limitIp=security_settings.xui_limit_ip,
        totalGB=sub.volume_bytes,
        expiryTime=expiry_ms,
        enable=new_enable_status,
        subId=existing_sub_id,
        comment=xui_record.username or "",
    )

    try:
        server = ensure_inbound_server_loaded(xui_record.inbound)
        async with create_xui_client_for_server(server) as xui_client:
            await xui_client.update_client(
                inbound_id=xui_record.inbound.xui_inbound_remote_id,
                client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                client=updated_client,
            )
        xui_record.is_active = new_enable_status
        await session.flush()
        
        status_text = "روشن" if new_enable_status else "خاموش"
        await safe_edit_or_send(callback, f"✅ کانفیگ با موفقیت {status_text} شد. برای اعمال تغییرات از لیست کانفیگ‌ها رفرش کنید.")
    except Exception as exc:
        logger.error("Failed to toggle enable for sub %s: %s", sub.id, exc)
        await safe_edit_or_send(callback, "❌ خطا در اجرای درخواست (برقراری ارتباط با سرور ناموفق بود).")





@router.callback_query(MyConfigCallback.filter(F.action == "cancel_refund"))
async def cancel_and_refund_config(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
) -> None:
    """Cancel an unused config and refund to wallet."""
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.plan),
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
        .with_for_update()
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد.")
        return

    if sub.status != "pending_activation" or sub.used_bytes > 0:
        await safe_edit_or_send(callback, "این کانفیگ قابل بازپرداخت نیست (قبلاً استفاده شده).")
        return

    # Block refund for ready configs — they are pre-made and cannot be returned
    from models.ready_config import ReadyConfigItem
    is_ready_config = await session.scalar(
        select(ReadyConfigItem.id).where(ReadyConfigItem.subscription_id == sub.id)
    )
    if is_ready_config:
        await safe_edit_or_send(callback, "❌ کانفیگ‌های آماده قابل لغو و بازپرداخت نیستند.")
        return

    # Delete from X-UI first — only refund if successful
    xui_record = sub.xui_client
    xui_deleted = True
    if xui_record and xui_record.inbound and xui_record.inbound.server:
        server_obj = xui_record.inbound.server
        # Skip X-UI deletion if server is deleted or inactive
        if server_obj.health_status == "deleted" or not server_obj.is_active:
            xui_record.is_active = False
        else:
            try:
                from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded
                server = ensure_inbound_server_loaded(xui_record.inbound)
                async with create_xui_client_for_server(server) as xui_client:
                    await xui_client.delete_client(
                        inbound_id=xui_record.inbound.xui_inbound_remote_id,
                        client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                    )
                xui_record.is_active = False
            except Exception as exc:
                logger.error("Failed to delete X-UI client on refund: %s", exc, exc_info=True)
                xui_deleted = False
                xui_error = str(exc)[:150]

    if not xui_deleted:
        await safe_edit_or_send(callback, f"❌ خطا در حذف کانفیگ از سرور:\n{xui_error}")
        return

    # Refund to wallet
    from decimal import Decimal
    from services.wallet.manager import WalletManager
    from models.order import Order

    # Find the order for this subscription using its direct FK
    order = None
    if sub.order_id:
        order = await session.get(Order, sub.order_id)
    refund_amount = order.amount if order else (sub.plan.price if sub.plan else Decimal("0"))

    if refund_amount and refund_amount > 0:
        wallet_manager = WalletManager(session)
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(refund_amount)),
            transaction_type="refund",
            direction="credit",
            currency="USD",
            reference_type="subscription",
            reference_id=sub.id,
            description="Refund for cancelled unused config",
            metadata={"subscription_id": str(sub.id)},
        )
        if order:
            order.status = "refunded"

    sub.status = "refunded"
    sub.sub_link = None
    await session.flush()

    from core.formatting import format_price
    await safe_edit_or_send(callback, 
        f"✅ کانفیگ لغو و مبلغ {format_price(refund_amount)} دلار به کیف پول برگشت داده شد."
    )


@router.callback_query(MyConfigCallback.filter(F.action == "delete"))
async def delete_expired_config(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
) -> None:
    """Delete an expired config (no refund)."""
    await callback.answer()
    if callback.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد.")
        return

    # Delete from X-UI (skip if server is deleted/inactive)
    xui_record = sub.xui_client
    if xui_record and xui_record.inbound and xui_record.inbound.server:
        server_obj = xui_record.inbound.server
        if server_obj.health_status != "deleted" and server_obj.is_active:
            try:
                from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded
                server = ensure_inbound_server_loaded(xui_record.inbound)
                async with create_xui_client_for_server(server) as xui_client:
                    await xui_client.delete_client(
                        inbound_id=xui_record.inbound.xui_inbound_remote_id,
                        client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                    )
            except Exception as exc:
                logger.error("Failed to delete X-UI client: %s", exc)
        xui_record.is_active = False

    sub.status = "cancelled"
    sub.sub_link = None
    await session.flush()

    await safe_edit_or_send(callback, "✅ کانفیگ حذف شد.")
