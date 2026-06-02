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
from apps.bot.utils.button_style import styled_button
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.utils.messaging import safe_edit_or_send
from apps.bot.states.my_configs import InboundChangeStates, UserConfigSearchStates
from core.formatting import escape_markdown, format_usage_bar, format_volume_bytes
from core.texts import Buttons
from apps.bot.utils.menu_match import MenuText
from models.subscription import Subscription
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.banner import create_traffic_banner
from services.provisioning.manager import (
    MigrationError,
    MigrationResult,
    ProvisioningManager,
)
from services.xui.runtime import build_vless_uri


logger = logging.getLogger(__name__)

router = Router(name="user-my-configs")

_ACTIVE_STATUSES = {"pending_activation", "active", "expired", "disabled"}
# Two-column list, so 16 fit comfortably per page (8 rows) → far fewer pages.
_CONFIGS_PAGE_SIZE = 16


class MyConfigCallback(CallbackData, prefix="myconfig"):
    action: str
    subscription_id: UUID


class MyConfigListCallback(CallbackData, prefix="myconfigs"):
    action: str
    page: int = 0


class InboundPickCallback(CallbackData, prefix="ibpick"):
    """Pick a target inbound during the 'change inbound' flow.

    The subscription_id is held in FSM state — only the inbound is in
    callback_data so we stay safely under Telegram's 64-byte limit.
    """
    inbound_id: UUID


class InboundConfirmCallback(CallbackData, prefix="ibok"):
    """Final confirm to actually perform the migration to ``inbound_id``."""
    inbound_id: UUID


@router.message(MenuText(Buttons.MY_CONFIGS))
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
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="🛒 خرید اولین سرویس", callback_data="user:buy")
        empty_kb.button(text="🎁 دریافت کانفیگ تست", callback_data="user:free_trial")
        empty_kb.adjust(1)
        await message.answer(
            "📭 هنوز هیچ سرویس فعالی ندارید.\n\n"
            "برای شروع، یک پلن تهیه کنید یا کانفیگ تست رایگان دریافت کنید.",
            reply_markup=empty_kb.as_markup(),
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
        # Color by status: finished/disabled → red, active → green, else blue.
        if sub.status in ("expired", "disabled"):
            role = "destructive"
        elif sub.status == "active":
            role = "confirm"
        else:
            role = "navigation"
        styled_button(
            builder,
            _build_config_button_label(sub),
            callback_data=MyConfigCallback(
                action=f"viewp{page}",
                subscription_id=sub.id,
            ).pack(),
            role=role,
        )
    # Two columns so more configs fit per page.
    builder.adjust(2)

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
    # For configs imported from the legacy bot, we don't have an XUIClientRecord
    # (those configs live on the OLD operator's panels). Their identity sits on
    # the Subscription itself in `legacy_remark` — the original config name we
    # preserve byte-for-byte for the eventual migrate-to-new-inbound flow.
    if sub.source == "imported_legacy" and sub.legacy_remark:
        config_name = sub.legacy_remark
    elif sub.xui_client:
        config_name = sub.xui_client.username
    elif sub.plan:
        config_name = sub.plan.name
    else:
        config_name = "نامشخص"

    status_emoji = {"active": "✅", "pending_activation": "⏳", "expired": "❌", "disabled": "🚫"}.get(sub.status, "❓")
    # Visual marker so the user can tell at a glance which configs are
    # carry-overs from the previous bot vs new ones on this bot.
    if sub.source == "imported_legacy":
        status_emoji = f"🗂 {status_emoji}"
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

    # ── Imported-from-legacy subs render a separate, simpler view ─────────
    # They have no XUIClientRecord on our side (the original config lived
    # on the old operator's panels). All we can show is the carried-over
    # metadata + the original VLESS link. The action set is also different:
    # there's no usage sync, no real renewal — only "transfer to an inbound
    # this operator allows", which we wire below.
    if sub.source == "imported_legacy":
        await _render_imported_sub_detail(callback, sub)
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

        # Auto-renew toggle — only meaningful for plan-based services (we extend
        # by the plan's duration). Green when ON, neutral when OFF.
        if sub.plan_id is not None:
            ar_on = bool(getattr(sub, "auto_renew_enabled", False))
            styled_button(
                builder,
                "🔁 تمدید خودکار: روشن ✅" if ar_on else "🔁 تمدید خودکار: خاموش",
                callback_data=MyConfigCallback(action="toggle_autorenew", subscription_id=sub.id).pack(),
                role="confirm" if ar_on else "navigation",
            )

    if sub.status in ("active", "pending_activation") and not server_deleted:
        # New Feature: Change Link / Reset UUID
        builder.button(text="🔄 تغییر لینک", callback_data=MyConfigCallback(action="reset_uuid", subscription_id=sub.id).pack())

        # New Feature: Move to another inbound (works around DPI / dead-server issues).
        builder.button(
            text="🛠 تغییر سرور (رفع اتصال)",
            callback_data=MyConfigCallback(action="change_inbound", subscription_id=sub.id).pack(),
        )

        # New Feature: Toggle enable status — red when it turns the config OFF,
        # green when it turns it back ON (overrides the callback heuristic, which
        # would otherwise see "enable" and color it green either way).
        is_enabled = xui.is_active if xui else False
        toggle_text = "🔴 خاموش کردن" if is_enabled else "🟢 روشن کردن"
        styled_button(
            builder,
            toggle_text,
            callback_data=MyConfigCallback(action="toggle_enable", subscription_id=sub.id).pack(),
            role="destructive" if is_enabled else "confirm",
        )

    # Load user actions settings once for all toggle checks
    user_actions_settings = await AppSettingsRepository(session).get_user_actions_settings()

    if sub.status in ("active", "pending_activation") and not server_deleted:
        # Transfer config to another user — only if admin enabled
        if user_actions_settings.transfer_enabled:
            builder.button(text="🔀 انتقال کانفیگ", callback_data=MyConfigCallback(action="transfer", subscription_id=sub.id).pack())

    # Cancel & refund for unused configs — but NOT for ready configs (they can't be returned)
    # Only show if admin has enabled refund
    if user_actions_settings.refund_enabled and sub.status == "pending_activation" and sub.used_bytes == 0:
        from models.ready_config import ReadyConfigItem
        is_ready_config = await session.scalar(
            select(ReadyConfigItem.id).where(ReadyConfigItem.subscription_id == sub.id)
        )
        if not is_ready_config:
            builder.button(text="🔄 لغو و بازپرداخت", callback_data=MyConfigCallback(action="cancel_refund", subscription_id=sub.id).pack())
    # Delete: expired configs OR configs with deleted/missing server
    # Only show if admin has enabled delete
    if user_actions_settings.delete_enabled and (sub.status == "expired" or server_deleted):
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


@router.callback_query(MyConfigCallback.filter(F.action == "toggle_autorenew"))
async def toggle_autorenew_handler(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Flip the per-service auto-renew opt-in, then re-render the detail."""
    if callback.from_user is None:
        await callback.answer()
        return
    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer()
        return
    sub = await session.scalar(
        select(Subscription).where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return
    sub.auto_renew_enabled = not bool(getattr(sub, "auto_renew_enabled", False))
    await session.flush()
    # Re-render the detail (my_config_detail_handler answers the callback and
    # redraws the keyboard, so the toggle button shows its new state).
    await my_config_detail_handler(
        callback,
        MyConfigCallback(action="view", subscription_id=sub.id),
        session,
        bot,
    )


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

    # Check if refund is enabled
    user_actions = await AppSettingsRepository(session).get_user_actions_settings()
    if not user_actions.refund_enabled:
        await safe_edit_or_send(callback, "❌ بازپرداخت توسط مدیر غیرفعال شده است.")
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

    # Check if delete is enabled
    user_actions = await AppSettingsRepository(session).get_user_actions_settings()
    if not user_actions.delete_enabled:
        await safe_edit_or_send(callback, "❌ حذف کانفیگ توسط مدیر غیرفعال شده است.")
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


# ─────────────────────────────────────────────────────────────────────────────
#  Change-Inbound Flow
# ─────────────────────────────────────────────────────────────────────────────
#
# When the user taps "🛠 تغییر سرور (رفع اتصال)" on a config's detail page we
# walk them through three steps:
#   1) show the list of available inbounds (= active, server active, not the
#      one their config is already on)
#   2) confirm the destination
#   3) run migrate_subscription_to_inbound() and report the new sub_link.
#
# Everything that touches X-UI is wrapped in a savepoint inside the service
# layer — see services/provisioning/manager.py::migrate_subscription_to_inbound.

_INBOUND_PICK_PAGE_SIZE = 8


def _format_inbound_label(inbound: XUIInboundRecord) -> str:
    """Human label for the picker keyboard."""
    parts: list[str] = []
    if inbound.server is not None and inbound.server.name:
        parts.append(inbound.server.name)
    if inbound.remark:
        parts.append(inbound.remark)
    elif inbound.tag:
        parts.append(inbound.tag)
    elif inbound.xui_inbound_remote_id is not None:
        parts.append(f"#{inbound.xui_inbound_remote_id}")
    proto_port: list[str] = []
    if inbound.protocol:
        proto_port.append(str(inbound.protocol))
    if inbound.port:
        proto_port.append(str(inbound.port))
    label = " · ".join(parts) if parts else "اینباند"
    if proto_port:
        label = f"{label} ({':'.join(proto_port)})"
    # Cap so the final button text doesn't blow past 48 visible chars.
    return label[:48]


async def _list_available_inbounds(
    session: AsyncSession,
    *,
    exclude_inbound_id: UUID | None,
) -> list[XUIInboundRecord]:
    """Inbounds the user can migrate TO.

    Selection rules:
      * inbound is active + its server is active
      * not the user's current inbound
      * if the admin has marked specific inbounds as "migration targets"
        (via the admin panel: 🖥 مدیریت سرورها → ⚙️ اینباندهای fallback),
        only those are eligible. Otherwise every active inbound is
        offered — the safe fallback for fresh installs.
    """
    settings_repo = AppSettingsRepository(session)
    allowed_raw = await settings_repo.get_migration_target_inbound_ids()
    allowed_ids: list[UUID] = []
    for raw in allowed_raw:
        try:
            allowed_ids.append(UUID(raw))
        except ValueError:
            logger.warning("Ignoring invalid migration-target inbound id: %r", raw)

    stmt = (
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .join(XUIServerRecord, XUIInboundRecord.server_id == XUIServerRecord.id)
        .where(
            XUIInboundRecord.is_active.is_(True),
            XUIServerRecord.is_active.is_(True),
        )
        .order_by(XUIServerRecord.priority.asc(), XUIInboundRecord.created_at.asc())
    )
    if exclude_inbound_id is not None:
        stmt = stmt.where(XUIInboundRecord.id != exclude_inbound_id)
    if allowed_ids:
        stmt = stmt.where(XUIInboundRecord.id.in_(allowed_ids))

    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


@router.callback_query(MyConfigCallback.filter(F.action == "change_inbound"))
async def change_inbound_start(
    callback: CallbackQuery,
    callback_data: MyConfigCallback,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Step 1 — show the inbound picker."""
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
            selectinload(Subscription.xui_client).selectinload(XUIClientRecord.inbound),
        )
        .where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد یا متعلق به شما نیست.")
        return
    # Imported-legacy subs can be migrated regardless of status (an
    # "expired" import just means the legacy expiry passed — but the
    # operator may still want to re-provision it on a new inbound). For
    # native subs we keep the original guard.
    if sub.source != "imported_legacy" and sub.status not in ("active", "pending_activation"):
        await safe_edit_or_send(
            callback,
            "این سرویس قابل انتقال نیست. فقط کانفیگ‌های فعال یا منتظر فعال‌سازی منتقل می‌شوند.",
        )
        return
    # Imported-from-legacy subs are allowed to migrate to ANY admin-
    # allowed inbound (they have no current X-UI client to exclude).
    if sub.xui_client is None and sub.source != "imported_legacy":
        await safe_edit_or_send(callback, "این کانفیگ روی پنل X-UI ثبت نشده است.")
        return

    current_inbound_id = sub.xui_client.inbound_id if sub.xui_client else None
    inbounds = await _list_available_inbounds(session, exclude_inbound_id=current_inbound_id)
    if not inbounds:
        await safe_edit_or_send(
            callback,
            "❌ هیچ اینباند دیگری برای انتقال وجود ندارد.\n\n"
            "لطفاً با پشتیبانی تماس بگیرید.",
        )
        return

    await state.clear()
    await state.set_state(InboundChangeStates.picking_inbound)
    await state.update_data(migrate_sub_id=str(sub.id))

    builder = InlineKeyboardBuilder()
    for inbound in inbounds[:_INBOUND_PICK_PAGE_SIZE]:
        builder.button(
            text=_format_inbound_label(inbound),
            callback_data=InboundPickCallback(inbound_id=inbound.id).pack(),
        )
    builder.button(
        text="❌ انصراف",
        callback_data=MyConfigCallback(action="view", subscription_id=sub.id).pack(),
    )
    builder.adjust(1)

    remaining = max(sub.volume_bytes - sub.used_bytes, 0)
    await safe_edit_or_send(
        callback,
        "🛠 <b>انتقال کانفیگ به اینباند دیگر</b>\n"
        "━━━━━━━━━━━━━━\n"
        "اگر کانفیگ شما متصل نمی‌شود می‌توانید آن را به یک اینباند سالم منتقل کنید.\n"
        f"💾 حجم باقی‌مانده‌ی شما: <b>{format_volume_bytes(remaining)}</b>\n"
        "⏱ تاریخ انقضا تغییری نمی‌کند.\n\n"
        "👇 اینباند مقصد را انتخاب کنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(InboundPickCallback.filter())
async def change_inbound_pick(
    callback: CallbackQuery,
    callback_data: InboundPickCallback,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    """Step 2 — user picked an inbound. Show a confirm prompt."""
    await callback.answer()
    if callback.from_user is None:
        return

    current_state = await state.get_state()
    if current_state != InboundChangeStates.picking_inbound.state:
        await safe_edit_or_send(
            callback,
            "این عملیات منقضی شده است. لطفاً از منوی سرویس‌های من دوباره شروع کنید.",
        )
        return

    data = await state.get_data()
    sub_id_raw = data.get("migrate_sub_id")
    if not sub_id_raw:
        await safe_edit_or_send(callback, "خطای داخلی. لطفاً دوباره تلاش کنید.")
        await state.clear()
        return
    try:
        sub_id = UUID(sub_id_raw)
    except ValueError:
        await safe_edit_or_send(callback, "خطای داخلی. لطفاً دوباره تلاش کنید.")
        await state.clear()
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "حساب شما پیدا نشد.")
        await state.clear()
        return

    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client).selectinload(XUIClientRecord.inbound),
        )
        .where(Subscription.id == sub_id, Subscription.user_id == user.id)
    )
    if sub is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد.")
        await state.clear()
        return
    # Imported-legacy subs intentionally have NO `xui_client` — the original
    # lived on the previous bot's panels. Their migrate path provisions a
    # fresh X-UI client. Only native subs require an existing xui_client.
    is_imported = (sub.source == "imported_legacy")
    if not is_imported and sub.xui_client is None:
        await safe_edit_or_send(callback, "کانفیگ پیدا نشد.")
        await state.clear()
        return

    inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.id == callback_data.inbound_id)
    )
    if inbound is None or not inbound.is_active:
        await safe_edit_or_send(callback, "اینباند انتخاب‌شده فعال نیست.")
        return
    # "Same inbound" guard doesn't apply to imported subs — they have
    # nothing on our side yet, so any target is a fresh move.
    if not is_imported and sub.xui_client is not None and inbound.id == sub.xui_client.inbound_id:
        await safe_edit_or_send(callback, "این سرویس از قبل روی همین اینباند است.")
        return

    await state.set_state(InboundChangeStates.confirming)

    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ بله، انتقال بده",
        callback_data=InboundConfirmCallback(inbound_id=inbound.id).pack(),
    )
    builder.button(
        text="↩️ بازگشت",
        callback_data=MyConfigCallback(action="change_inbound", subscription_id=sub.id).pack(),
    )
    builder.adjust(1)

    remaining = max(sub.volume_bytes - sub.used_bytes, 0)
    if is_imported:
        # No X-UI client on our side; the "old" inbound was the previous
        # bot's panel. Show a hint instead of a real label.
        old_label = "🗂 (ربات قبلی)"
    else:
        old_label = (
            _format_inbound_label(sub.xui_client.inbound)
            if sub.xui_client is not None and sub.xui_client.inbound is not None
            else "نامشخص"
        )
    new_label = _format_inbound_label(inbound)

    await safe_edit_or_send(
        callback,
        "⚠️ <b>تأیید انتقال کانفیگ</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"از: <code>{old_label}</code>\n"
        f"به: <code>{new_label}</code>\n"
        f"💾 حجم منتقل‌شونده: <b>{format_volume_bytes(remaining)}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<i>⚡ بعد از انتقال یک لینک جدید دریافت می‌کنید و باید آن را در اپ "
        "خود وارد کنید. لینک قبلی غیرفعال می‌شود.</i>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(InboundConfirmCallback.filter())
async def change_inbound_execute(
    callback: CallbackQuery,
    callback_data: InboundConfirmCallback,
    session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Step 3 — run the migration."""
    await callback.answer("⏳ در حال انتقال…")
    if callback.from_user is None:
        return

    current_state = await state.get_state()
    if current_state != InboundChangeStates.confirming.state:
        await safe_edit_or_send(
            callback,
            "این عملیات منقضی شده است. لطفاً دوباره از سرویس‌های من شروع کنید.",
        )
        return

    data = await state.get_data()
    sub_id_raw = data.get("migrate_sub_id")
    if not sub_id_raw:
        await safe_edit_or_send(callback, "خطای داخلی. لطفاً دوباره تلاش کنید.")
        await state.clear()
        return
    try:
        sub_id = UUID(sub_id_raw)
    except ValueError:
        await safe_edit_or_send(callback, "خطای داخلی.")
        await state.clear()
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "حساب شما پیدا نشد.")
        await state.clear()
        return

    # Ownership guard — the migration service doesn't know which telegram
    # user is asking; we enforce that here.
    owned = await session.scalar(
        select(Subscription.id).where(
            Subscription.id == sub_id,
            Subscription.user_id == user.id,
        )
    )
    if owned is None:
        await safe_edit_or_send(callback, "کانفیگ متعلق به شما نیست.")
        await state.clear()
        return

    # Pick the right migration path. Imported-legacy subs have no X-UI
    # client on our side and need a fresh provision; native subs swap
    # an existing client between inbounds.
    sub_source = await session.scalar(
        select(Subscription.source).where(Subscription.id == sub_id),
    )
    try:
        mgr = ProvisioningManager(session)
        if sub_source == "imported_legacy":
            result: MigrationResult = await mgr.migrate_imported_subscription_to_inbound(
                subscription_id=sub_id,
                target_inbound_id=callback_data.inbound_id,
            )
        else:
            result = await mgr.migrate_subscription_to_inbound(
                subscription_id=sub_id,
                target_inbound_id=callback_data.inbound_id,
            )
    except MigrationError as exc:
        await safe_edit_or_send(callback, f"❌ {exc}")
        await state.clear()
        return
    except Exception as exc:
        logger.error("Unexpected migration error for sub %s: %s", sub_id, exc, exc_info=True)
        await safe_edit_or_send(
            callback,
            "❌ خطای ناشناخته‌ای رخ داد. لطفاً با پشتیبانی تماس بگیرید.",
        )
        await state.clear()
        return

    await state.clear()

    # Audit log so admins can trace which user migrated which sub and when.
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=user.id,
            action="user_inbound_migration",
            entity_type="subscription",
            entity_id=sub_id,
            payload={
                "old_inbound": result.old_inbound_label,
                "new_inbound": result.new_inbound_label,
                "remaining_bytes": result.remaining_bytes,
            },
        )
    except Exception as exc:
        logger.warning("Audit log failed for migration %s: %s", sub_id, exc)

    # Build the success message + import buttons.
    import html as _html
    from core.config import settings as _settings
    base = _settings.web_base_url.rstrip("/")
    encoded_uri = urllib.parse.quote(result.new_vless_uri, safe="")
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 v2rayNG (اندروید)", url=f"{base}/api/dl/v2rayng?url={encoded_uri}")
    builder.button(text="🍎 Shadowrocket (آیفون)", url=f"{base}/api/dl/shadowrocket?url={encoded_uri}")
    builder.button(text="📦 V2Box (هر دو)", url=f"{base}/api/dl/v2box?url={encoded_uri}")
    share_text = urllib.parse.quote(f"کانفیگ من:\n{result.new_sub_link}")
    builder.button(
        text="📤 اشتراک‌گذاری",
        url=f"https://t.me/share/url?url={urllib.parse.quote(result.new_sub_link)}&text={share_text}",
    )
    builder.button(
        text="📦 بازگشت به سرویس",
        callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack(),
    )
    builder.adjust(2, 2, 1)

    text = (
        "✅ <b>انتقال کانفیگ موفق بود!</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🚀 اینباند جدید: <b>{_html.escape(result.new_inbound_label)}</b>\n"
        f"💾 حجم باقی‌مانده: <b>{format_volume_bytes(result.remaining_bytes)}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "🔗 <b>لینک جدید (روی متن بزنید تا کپی شود):</b>\n"
        f"<code>{_html.escape(result.new_sub_link)}</code>\n\n"
        "📋 <b>کانفیگ مستقیم:</b>\n"
        f"<code>{_html.escape(result.new_vless_uri)}</code>\n\n"
        "⚠️ <i>لینک قبلی دیگر کار نمی‌کند — حتماً لینک جدید را در اپ خود "
        "جایگزین کنید.</i>"
    )

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


# ─────────────────────────────────────────────────────────────────────────
#  Imported-from-legacy-bot config viewer
# ─────────────────────────────────────────────────────────────────────────
#
# These rows came in via `scripts/import_legacy.py` from the previous
# operator's MySQL bot. We don't own the X-UI client on the other side,
# so this view is read-only metadata + a CTA to migrate onto an
# admin-allowed inbound (preserving the original config name verbatim).

async def _render_imported_sub_detail(callback: CallbackQuery, sub: Subscription) -> None:
    import html as _html

    remark = sub.legacy_remark or "نامشخص"
    legacy_link = sub.legacy_link or sub.sub_link or ""
    volume_label = format_volume_bytes(sub.volume_bytes) if sub.volume_bytes else "نامشخص"

    if sub.ends_at is not None:
        now = datetime.now(timezone.utc)
        ea = sub.ends_at if sub.ends_at.tzinfo else sub.ends_at.replace(tzinfo=timezone.utc)
        remaining_days = (ea - now).days
        if remaining_days > 0:
            time_label = f"{remaining_days} روز مانده"
        else:
            time_label = "منقضی شده ❌"
    else:
        time_label = "نامحدود / نامشخص"

    status_label = {
        "active": "✅ فعال",
        "pending_activation": "⏳ منتظر فعال‌سازی",
        "expired": "❌ منقضی",
        "disabled": "🚫 غیرفعال",
    }.get(sub.status, sub.status)

    lines = [
        "🗂 <b>کانفیگ منتقل‌شده از ربات قبلی</b>",
        "━━━━━━━━━━━━━━",
        f"📛 نام: <code>{_html.escape(remark)}</code>",
        f"💾 حجم: <code>{_html.escape(volume_label)}</code>",
        f"📅 زمان: <code>{_html.escape(time_label)}</code>",
        f"🔄 وضعیت: {status_label}",
        "",
    ]
    if legacy_link:
        lines.append("🔗 <b>لینک قدیمی</b> (ممکن است دیگر کار نکند):")
        lines.append(f"<code>{_html.escape(legacy_link)}</code>")
        lines.append("")
    lines.append(
        "ℹ️ این کانفیگ از ربات قبلی منتقل شده. برای استفاده‌ی پایدار، آن را "
        "به یکی از اینباندهای جدید انتقال دهید. <b>نام کانفیگ حفظ می‌شود</b>."
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 انتقال به اینباند جدید",
        callback_data=MyConfigCallback(action="change_inbound", subscription_id=sub.id).pack(),
    )
    builder.button(
        text="🔙 بازگشت",
        callback_data="myconfig:back_to_list",
    )
    builder.adjust(1)

    await safe_edit_or_send(callback, "\n".join(lines), reply_markup=builder.as_markup())
