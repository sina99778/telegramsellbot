"""
Admin global config search + assign-to-any-member.

Lets an admin search EVERY config in the system (by config name / email / UUID /
sub-link, or by the current owner's username / telegram-id), open any result,
and re-assign it onto any member's account. Ownership-only move — the panel
client (and therefore the working sub-link) is left untouched, exactly like the
per-user transfer in apps/bot/handlers/admin/users.py. The heavy lifting is the
single shared service in services/admin_transfer.py.

No slash command — reachable only via the "جستجوی کانفیگ" button on the admin
main menu (operator preference: buttons, not commands).
"""
from __future__ import annotations

import logging
from html import escape as _html_escape
from math import ceil
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import ConfigSearchStates
from apps.bot.utils.messaging import safe_edit_or_send
from core.formatting import format_usage_bar, format_volume_bytes
from core.texts import AdminButtons
from models.subscription import Subscription
from models.user import User
from services.admin_transfer import (
    AdminTransferError,
    admin_transfer_configs,
    config_search_label,
    owner_label,
    resolve_target_user,
    search_configs,
    status_fa,
)


logger = logging.getLogger(__name__)

router = Router(name="admin-config-search")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

_PAGE_SIZE = 8


class CfgSearchPageCallback(CallbackData, prefix="acfgsp"):
    page: int


class CfgSearchPickCallback(CallbackData, prefix="acfgpk"):
    sub_id: UUID


class CfgAssignCallback(CallbackData, prefix="acfgas"):
    sub_id: UUID


# ─── Views (pure: build text + markup) ────────────────────────────────────────


def _results_view(rows: list[Subscription], total: int, query: str, page: int):
    total_pages = max(ceil(total / _PAGE_SIZE), 1)
    shown = _html_escape(query) if query.strip() else "همه‌ی کانفیگ‌ها"
    text = (
        "🔎 <b>نتایج جستجوی کانفیگ</b>\n"
        f"عبارت: <code>{shown}</code>\n"
        f"یافت شد: <b>{total}</b> کانفیگ · صفحه {page + 1}/{total_pages}\n\n"
        "روی هر کانفیگ بزن تا جزئیات و انتقالش را ببینی."
    )

    builder = InlineKeyboardBuilder()
    for sub in rows:
        builder.button(
            text=config_search_label(sub)[:64],
            callback_data=CfgSearchPickCallback(sub_id=sub.id).pack(),
        )

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("◀️ قبلی", CfgSearchPageCallback(page=page - 1).pack()))
    nav.append((f"📄 {page + 1}/{total_pages}", "pagination:noop"))
    if page < total_pages - 1:
        nav.append(("بعدی ▶️", CfgSearchPageCallback(page=page + 1).pack()))
    for label, cb in nav:
        builder.button(text=label, callback_data=cb)

    builder.button(text="🔎 جستجوی جدید", callback_data="admin:config_search")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")

    layout = [1] * len(rows) + [len(nav), 1, 1]
    builder.adjust(*layout)
    return text, builder.as_markup()


def _no_results_view(query: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 جستجوی مجدد", callback_data="admin:config_search")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    return (
        f"❌ هیچ کانفیگی برای «<code>{_html_escape(query)}</code>» پیدا نشد.\n"
        "عبارتِ دیگری بفرست یا برای دیدنِ همه یک <code>*</code> بفرست.",
        builder.as_markup(),
    )


def _sub_detail_view(sub: Subscription):
    owner = getattr(sub, "user", None)
    plan = getattr(sub, "plan", None)
    xc = getattr(sub, "xui_client", None)

    name = (
        (getattr(xc, "username", None) if xc else None)
        or (getattr(xc, "email", None) if xc else None)
        or (getattr(plan, "name", None) if plan else None)
        or f"config-{str(sub.id)[:8]}"
    )
    used = format_volume_bytes(sub.used_bytes)
    total_v = format_volume_bytes(sub.volume_bytes) if sub.volume_bytes else "نامحدود"
    link = sub.sub_link or (getattr(xc, "sub_link", None) if xc else None)
    owner_id = f"<code>{owner.telegram_id}</code>" if owner is not None else "—"

    lines = [
        "📦 <b>جزئیات کانفیگ</b>",
        f"🏷 نام: <b>{_html_escape(str(name))}</b>",
        f"📊 وضعیت: {status_fa(sub.status)}",
        f"👤 مالکِ فعلی: {_html_escape(owner_label(owner))} (آی‌دی: {owner_id})",
        f"💾 مصرف: {used} / {total_v}",
    ]
    if sub.volume_bytes:
        lines.append(format_usage_bar(sub.used_bytes, sub.volume_bytes))
    if plan is not None:
        lines.append(f"📦 پلن: {_html_escape(plan.name)}")
    if sub.ends_at:
        lines.append(f"📅 انقضا: {sub.ends_at.strftime('%Y-%m-%d %H:%M')}")
    if link:
        lines.append(f"🔗 لینک:\n<code>{_html_escape(str(link))}</code>")

    builder = InlineKeyboardBuilder()
    builder.button(
        text="📥 انتقال به اکانتِ دیگر",
        callback_data=CfgAssignCallback(sub_id=sub.id).pack(),
    )
    builder.button(text="🔙 بازگشت به نتایج", callback_data=CfgSearchPageCallback(page=0).pack())
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


# ─── Search flow ──────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:config_search")
async def config_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ConfigSearchStates.waiting_for_query)
    await state.update_data(cfgsearch_query="")
    await safe_edit_or_send(
        callback,
        "🔎 <b>جستجوی کانفیگ‌ها</b>\n\n"
        "بخشی از مشخصاتِ کانفیگ را بفرست:\n"
        "• نام/یوزرنیم کانفیگ، ایمیل، UUID یا لینک\n"
        "• یا یوزرنیم/آی‌دیِ مالکِ فعلیِ کانفیگ\n\n"
        "برای دیدنِ <b>همه‌ی کانفیگ‌ها</b> یک ستاره <code>*</code> بفرست.\n"
        "برای لغو /cancel بزن.",
        parse_mode="HTML",
    )


@router.message(ConfigSearchStates.waiting_for_query)
async def config_search_run(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    q = message.text.strip()
    if q.lower() == "/cancel":
        await state.clear()
        await message.answer("لغو شد.")
        return

    await state.update_data(cfgsearch_query=q)
    rows, total = await search_configs(session, q, limit=_PAGE_SIZE, offset=0)
    if total == 0:
        text, markup = _no_results_view(q)
    else:
        text, markup = _results_view(rows, total, q, 0)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(CfgSearchPageCallback.filter())
async def config_search_page(
    callback: CallbackQuery,
    callback_data: CfgSearchPageCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    data = await state.get_data()
    q = data.get("cfgsearch_query", "") or ""
    page = max(callback_data.page, 0)
    rows, total = await search_configs(session, q, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    if total == 0:
        text, markup = _no_results_view(q)
    else:
        text, markup = _results_view(rows, total, q, page)
    await safe_edit_or_send(callback, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(CfgSearchPickCallback.filter())
async def config_search_pick(
    callback: CallbackQuery,
    callback_data: CfgSearchPickCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    sub = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client),
            selectinload(Subscription.plan),
            selectinload(Subscription.user),
        )
        .where(Subscription.id == callback_data.sub_id)
    )
    if sub is None:
        await safe_edit_or_send(callback, "❌ کانفیگ یافت نشد.")
        return
    text, markup = _sub_detail_view(sub)
    await safe_edit_or_send(callback, text, reply_markup=markup, parse_mode="HTML")


# ─── Assign flow ──────────────────────────────────────────────────────────────


@router.callback_query(CfgAssignCallback.filter())
async def config_assign_prompt(
    callback: CallbackQuery,
    callback_data: CfgAssignCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    sub = await session.scalar(
        select(Subscription).where(Subscription.id == callback_data.sub_id)
    )
    if sub is None:
        await safe_edit_or_send(callback, "❌ کانفیگ یافت نشد.")
        return

    await state.set_state(ConfigSearchStates.waiting_for_assign_target)
    await state.update_data(
        cfgassign_sub_id=str(sub.id),
        cfgassign_source_user_id=str(sub.user_id),
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ لغو", callback_data="acfg:cancel")
    await safe_edit_or_send(
        callback,
        "📥 <b>انتقالِ کانفیگ به اکانتِ دیگر</b>\n\n"
        "آی‌دی عددی تلگرام یا یوزرنیم (بدون @) اکانتِ <b>مقصد</b> را بفرست.\n\n"
        "⚠️ اکانت مقصد باید عضو ربات باشد.\n"
        "ℹ️ لینکِ کانفیگ تغییر نمی‌کند؛ فقط مالکیت منتقل می‌شود.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.message(ConfigSearchStates.waiting_for_assign_target)
async def config_assign_target_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return
    if message.text.strip().lower() == "/cancel":
        await _reset_to_search(state)
        await message.answer("❌ انتقال لغو شد. می‌توانی دوباره جستجو کنی.")
        return

    data = await state.get_data()
    sub_id = data.get("cfgassign_sub_id")
    source_id = data.get("cfgassign_source_user_id")
    if not sub_id or not source_id:
        await state.clear()
        await message.answer("❌ نشست منقضی شد. دوباره از جستجو شروع کن.")
        return

    target = await resolve_target_user(session, message.text)
    if target is None:
        await message.answer("❌ کاربری با این مشخصات پیدا نشد. دوباره بفرست یا /cancel.")
        return
    if str(target.id) == str(source_id):
        await message.answer("ℹ️ این کانفیگ همین الان متعلق به همین کاربر است. یک اکانتِ دیگر بفرست.")
        return

    await state.update_data(cfgassign_target_user_id=str(target.id))

    target_display = (
        f"@{target.username}" if target.username else f"ID: <code>{target.telegram_id}</code>"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید انتقال", callback_data="acfg:confirm")
    builder.button(text="❌ لغو", callback_data="acfg:cancel")
    builder.adjust(1)
    await message.answer(
        "📥 <b>تأیید انتقالِ کانفیگ</b>\n\n"
        f"👤 مقصد: {target_display} ({_html_escape(target.first_name or '-')})\n\n"
        "ℹ️ لینکِ کانفیگ <u>تغییر نمی‌کند</u>؛ فقط مالکیت به اکانت مقصد منتقل می‌شود.\n\n"
        "تأیید می‌کنی؟",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "acfg:confirm")
async def config_assign_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    data = await state.get_data()
    sub_id = data.get("cfgassign_sub_id")
    source_id = data.get("cfgassign_source_user_id")
    target_id = data.get("cfgassign_target_user_id")
    await _reset_to_search(state)

    if not sub_id or not source_id or not target_id:
        await safe_edit_or_send(callback, "❌ اطلاعات انتقال یافت نشد.")
        return

    try:
        result = await admin_transfer_configs(
            session,
            source_user_id=UUID(str(source_id)),
            target_user_id=UUID(str(target_id)),
            subscription_ids=[UUID(str(sub_id))],
            actor_label=f"bot_admin:{admin_user.telegram_id}",
            actor_user_id=admin_user.id,
        )
    except AdminTransferError as exc:
        await safe_edit_or_send(callback, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("config assign failed: %s", exc, exc_info=True)
        await safe_edit_or_send(callback, "❌ خطا در انتقال. دوباره تلاش کن.")
        return

    done = InlineKeyboardBuilder()
    done.button(text="🔎 جستجوی دیگر", callback_data="admin:config_search")
    done.button(text=AdminButtons.BACK, callback_data="admin:main")
    done.adjust(1)
    await safe_edit_or_send(
        callback,
        "✅ <b>انتقال موفق</b>\n\n"
        f"کانفیگ به <b>{_html_escape(result['target_name'])}</b> منتقل شد.",
        reply_markup=done.as_markup(),
        parse_mode="HTML",
    )

    # Best-effort: tell the new owner.
    try:
        await callback.bot.send_message(
            result["target_telegram_id"],
            "🎁 یک کانفیگ توسط پشتیبانی به حساب شما اضافه شد.\n\n"
            "از بخش «📋 سرویس‌های من» می‌توانید آن را مشاهده کنید.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify assign recipient failed: %s", exc)


@router.callback_query(F.data == "acfg:cancel")
async def config_assign_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _reset_to_search(state)
    await safe_edit_or_send(
        callback,
        "❌ انتقال لغو شد. می‌توانی دوباره جستجو کنی یا از منو ادامه بدهی.",
    )


async def _reset_to_search(state: FSMContext) -> None:
    """Drop assign-specific keys but KEEP the search query so 'back to results'
    still works, and return to the query state so the admin can search again."""
    await state.update_data(
        cfgassign_sub_id=None,
        cfgassign_source_user_id=None,
        cfgassign_target_user_id=None,
    )
    await state.set_state(ConfigSearchStates.waiting_for_query)
