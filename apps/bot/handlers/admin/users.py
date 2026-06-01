from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from html import escape as _html_escape
from math import ceil
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import ManageUserStates
from core.formatting import format_volume_bytes, format_usage_bar
from core.texts import AdminButtons, AdminMessages
from models.order import Order
from models.subscription import Subscription
from models.user import User
from models.xui import XUIClientRecord
from repositories.audit import AuditLogRepository
from services.phone_verification import get_verified_phone
from services.wallet.manager import WalletManager
from apps.bot.utils.messaging import safe_edit_or_send


logger = logging.getLogger(__name__)

router = Router(name="admin-users")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

USER_PAGE_SIZE = 10


class AdminUserActionCallback(CallbackData, prefix="admin_user"):
    action: str
    user_id: UUID


class AdminUserListPageCallback(CallbackData, prefix="admin_userlist"):
    page: int


class AdminXferPickCallback(CallbackData, prefix="adm_xfer"):
    # scope="all" → move every config of the source user; scope="one" → just sub_id
    scope: str
    sub_id: UUID | None = None


# ─── Users Menu ───────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Show users management menu with list and search options."""
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 لیست کاربران", callback_data=AdminUserListPageCallback(page=1).pack())
    builder.button(text="🔍 جستجوی کاربر", callback_data="admin:users:search")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    if callback.message:
        try:
            await callback.message.edit_text("مدیریت کاربران:", reply_markup=builder.as_markup())
        except Exception:
            await safe_edit_or_send(callback, "مدیریت کاربران:", reply_markup=builder.as_markup())


# ─── User List (Paginated) ────────────────────────────────────────────────────


@router.callback_query(AdminUserListPageCallback.filter())
async def admin_users_list(
    callback: CallbackQuery,
    callback_data: AdminUserListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    page = max(callback_data.page, 1)

    total_users = int(
        await session.scalar(select(func.count()).select_from(User)) or 0
    )
    total_pages = max(ceil(total_users / USER_PAGE_SIZE), 1)
    offset = (page - 1) * USER_PAGE_SIZE

    result = await session.execute(
        select(User)
        .options(selectinload(User.wallet))
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(USER_PAGE_SIZE)
    )
    users = list(result.scalars().all())

    if not users:
        builder = InlineKeyboardBuilder()
        builder.button(text=AdminButtons.BACK, callback_data="admin:users")
        builder.adjust(1)
        await safe_edit_or_send(callback, "هیچ کاربری وجود ندارد.", reply_markup=builder.as_markup())
        return

    builder = InlineKeyboardBuilder()
    for user in users:
        name = user.first_name or user.username or str(user.telegram_id)
        balance = f"{user.wallet.balance:.2f}" if user.wallet else "0.00"
        status_icon = "🟢" if user.status == "active" else "🔴"
        builder.button(
            text=f"{status_icon} {name} | ${balance}",
            callback_data=AdminUserActionCallback(action="profile", user_id=user.id).pack(),
        )

    # Pagination
    nav_buttons = []
    if page > 1:
        nav_buttons.append(("⬅️ قبلی", AdminUserListPageCallback(page=page - 1).pack()))
    nav_buttons.append((f"📄 {page}/{total_pages}", "pagination:noop"))
    if page < total_pages:
        nav_buttons.append(("بعدی ➡️", AdminUserListPageCallback(page=page + 1).pack()))

    for text, cb in nav_buttons:
        builder.button(text=text, callback_data=cb)

    builder.button(text=AdminButtons.BACK, callback_data="admin:users")

    rows = [1] * len(users)
    rows.append(len(nav_buttons))
    rows.append(1)
    builder.adjust(*rows)

    if callback.message:
        try:
            await callback.message.edit_text(
                f"👥 لیست کاربران ({total_users} نفر):",
                reply_markup=builder.as_markup(),
            )
        except Exception:
            await safe_edit_or_send(callback,
                f"👥 لیست کاربران ({total_users} نفر):",
                reply_markup=builder.as_markup(),
            )


# ─── User Search ──────────────────────────────────────────────────────────────


@router.callback_query(F.data == "admin:users:search")
async def admin_users_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_telegram_id)
    await safe_edit_or_send(callback,
        "🔍 شناسه تلگرام (عددی) یا یوزرنیم کاربر را ارسال کنید.\n"
        "برای لغو /cancel بزنید."
    )


@router.message(ManageUserStates.waiting_for_telegram_id)
async def admin_users_lookup(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return

    query_text = message.text.strip().lstrip("@")

    user: User | None = None
    try:
        telegram_id = int(query_text)
        user = await session.scalar(
            select(User)
            .options(selectinload(User.wallet), selectinload(User.profile), selectinload(User.subscriptions))
            .where(User.telegram_id == telegram_id)
        )
    except ValueError:
        user = await session.scalar(
            select(User)
            .options(selectinload(User.wallet), selectinload(User.profile), selectinload(User.subscriptions))
            .where(func.lower(User.username) == query_text.lower())
        )

    if user is None or user.wallet is None:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔍 جستجوی مجدد", callback_data="admin:users:search")
        builder.button(text=AdminButtons.BACK, callback_data="admin:users")
        builder.adjust(1)
        await message.answer(AdminMessages.USER_NOT_FOUND, reply_markup=builder.as_markup())
        await state.clear()
        return

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await state.clear()
    await message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


# ─── User Profile (from list click) ──────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "profile"))
async def admin_user_profile_from_list(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile), selectinload(User.subscriptions))
        .where(User.id == callback_data.user_id)
    )
    if user is None or user.wallet is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await safe_edit_or_send(callback,
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


# ─── Edit Balance ─────────────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "edit_balance"))
async def admin_edit_balance_prompt(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_balance_adjustment)
    await state.update_data(target_user_id=str(callback_data.user_id))
    await safe_edit_or_send(callback, AdminMessages.ENTER_BALANCE_ADJUSTMENT)


@router.message(ManageUserStates.waiting_for_balance_adjustment)
async def admin_edit_balance_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    state_data = await state.get_data()
    raw_user_id = state_data.get("target_user_id")
    if raw_user_id is None:
        await state.clear()
        await message.answer(AdminMessages.USER_NOT_FOUND)
        return

    try:
        amount = Decimal(message.text.strip())
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    if amount == Decimal("0"):
        await message.answer(AdminMessages.AMOUNT_NOT_ZERO)
        return

    target_user_id = UUID(str(raw_user_id))
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile))
        .where(User.id == target_user_id)
    )
    if user is None or user.wallet is None:
        await state.clear()
        await message.answer(AdminMessages.USER_NOT_FOUND)
        return

    wallet_manager = WalletManager(session)
    direction = "credit" if amount > 0 else "debit"
    await wallet_manager.process_transaction(
        user_id=user.id,
        amount=abs(amount),
        transaction_type="admin_adjustment",
        direction=direction,
        currency="USD",
        reference_type="manual",
        reference_id=None,
        description="Admin wallet adjustment",
        metadata={"admin_action": "edit_balance"},
    )
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="adjust_balance",
        entity_type="user",
        entity_id=user.id,
        payload={"amount": str(amount), "telegram_id": user.telegram_id},
    )

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await session.refresh(user, attribute_names=["wallet"])
    if user.wallet is not None:
        await session.refresh(user.wallet)
    await state.clear()
    await message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


# ─── Set Personal Discount ────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "set_discount"))
async def admin_set_discount_prompt(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_personal_discount)
    await state.update_data(target_user_id=str(callback_data.user_id))
    await safe_edit_or_send(
        callback,
        "🏷 درصد تخفیف شخصی کاربر را وارد کنید (0 تا 100):\n"
        "مثال: 20 (یعنی ۲۰٪ تخفیف روی تمام خریدها)\n"
        "برای لغو /cancel بزنید."
    )


@router.message(ManageUserStates.waiting_for_personal_discount)
async def admin_set_discount_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    state_data = await state.get_data()
    raw_user_id = state_data.get("target_user_id")
    if raw_user_id is None:
        await state.clear()
        await message.answer("کاربر پیدا نشد.")
        return

    try:
        discount = int(message.text.strip())
        if not (0 <= discount <= 100):
            raise ValueError
    except ValueError:
        await message.answer("❌ لطفاً یک عدد بین 0 تا 100 وارد کنید.")
        return

    target_user_id = UUID(str(raw_user_id))
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile))
        .where(User.id == target_user_id)
    )
    if user is None:
        await state.clear()
        await message.answer(AdminMessages.USER_NOT_FOUND)
        return

    old_discount = user.personal_discount_percent
    user.personal_discount_percent = discount
    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="set_personal_discount",
        entity_type="user",
        entity_id=user.id,
        payload={"old": old_discount, "new": discount, "telegram_id": user.telegram_id},
    )

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await state.clear()
    await message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders)
        + f"\n\n✅ تخفیف شخصی به {discount}٪ تنظیم شد.",
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


# ─── Toggle Ban ───────────────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "toggle_ban"))
async def admin_toggle_ban(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile))
        .where(User.id == callback_data.user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    disabled_configs = 0
    if user.status == "banned":
        user.status = "active"
        user.is_bot_blocked = False
    else:
        user.status = "banned"
        user.is_bot_blocked = True
        # Banning must also cut the user's VPN — otherwise their provisioned
        # configs keep serving traffic on the panel. Mirrors the mini-app ban.
        from services.provisioning.manager import ProvisioningManager
        try:
            disabled_configs = await ProvisioningManager(session).disable_user_active_configs(user.id)
        except Exception as exc:
            logger.warning("toggle_ban: failed to disable configs for user %s: %s", user.id, exc)

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_ban",
        entity_type="user",
        entity_id=user.id,
        payload={"status": user.status, "is_bot_blocked": user.is_bot_blocked, "disabled_configs": disabled_configs},
    )

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await safe_edit_or_send(callback,
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


# ─── Send Message to User ─────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "send_msg"))
async def admin_send_msg_prompt(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_message_to_user)
    await state.update_data(target_user_id=str(callback_data.user_id))
    await safe_edit_or_send(callback,
        "📩 پیام مورد نظر خود را برای این کاربر ارسال کنید.\n"
        "برای لغو /cancel بزنید."
    )


@router.message(ManageUserStates.waiting_for_message_to_user)
async def admin_send_msg_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    state_data = await state.get_data()
    raw_user_id = state_data.get("target_user_id")
    if raw_user_id is None:
        await state.clear()
        await message.answer("کاربر پیدا نشد.")
        return

    target_user = await session.scalar(
        select(User).where(User.id == UUID(raw_user_id))
    )
    if target_user is None:
        await state.clear()
        await message.answer(AdminMessages.USER_NOT_FOUND)
        return

    try:
        await message.bot.send_message(
            target_user.telegram_id,
            f"📩 پیام از طرف مدیریت:\n\n{message.text}",
        )
        await message.answer("✅ پیام با موفقیت ارسال شد.")
    except TelegramForbiddenError:
        await message.answer("❌ کاربر ربات را بلاک کرده است.")
    except TelegramBadRequest as exc:
        await message.answer(f"❌ خطا در ارسال: {exc}")
    except Exception as exc:
        await message.answer(f"❌ خطای ناشناخته: {exc}")

    await state.clear()


# ─── Toggle Admin Role ────────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "toggle_admin"))
async def admin_user_toggle_admin(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()

    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile), selectinload(User.subscriptions))
        .where(User.id == callback_data.user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    from core.config import settings
    # Protect the owner from demotion. Guard on telegram_id AND role=="owner"
    # (matching the mini-app): the previous check only compared telegram_id, so
    # an owner whose telegram_id differs from the configured OWNER_TELEGRAM_ID
    # (env change / DB import) could be silently demoted to a normal user.
    if user.telegram_id == settings.owner_telegram_id or user.role == "owner":
        await safe_edit_or_send(callback, "❌ نقش مالک اصلی ربات (owner) قابل تغییر نیست.")
        return

    new_role = "admin" if user.role == "user" else "user"
    user.role = new_role
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_admin",
        entity_type="user",
        entity_id=user.id,
        payload={"new_role": new_role},
    )
    await session.flush()

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    status_text = "ادمین 👑" if new_role == "admin" else "کاربر عادی 👤"
    if callback.message:
        try:
            await callback.message.edit_text(
                _build_user_profile_text(user=user, total_orders=total_orders) + f"\n\n✅ نقش به {status_text} تغییر یافت.",
                reply_markup=_build_user_profile_keyboard(user.id, user.status),
                parse_mode="HTML",
            )
        except Exception:
            await safe_edit_or_send(callback,
                _build_user_profile_text(user=user, total_orders=total_orders) + f"\n\n✅ نقش به {status_text} تغییر یافت.",
                reply_markup=_build_user_profile_keyboard(user.id, user.status),
                parse_mode="HTML",
            )


# ─── Reset Free Trial ─────────────────────────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "reset_trial"))
async def admin_user_reset_trial(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.profile), selectinload(User.subscriptions))
        .where(User.id == callback_data.user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return
    user.has_received_free_trial = False
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="reset_trial",
        entity_type="user",
        entity_id=user.id,
        payload={"telegram_id": user.telegram_id},
    )
    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await safe_edit_or_send(
        callback,
        _build_user_profile_text(user=user, total_orders=total_orders) + "\n\n✅ محدودیت تست کاربر ریست شد.",
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
        parse_mode="HTML",
    )


@router.callback_query(AdminUserActionCallback.filter(F.action == "view_configs"))
async def view_user_configs(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
) -> None:
    """Redirect to the full paginated subscription management list in subs.py."""
    from apps.bot.handlers.admin.subs import _render_user_configs

    await callback.answer()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=1,
    )


# ─── Transfer configs to another account ──────────────────────────────────────


@router.callback_query(AdminUserActionCallback.filter(F.action == "transfer_configs"))
async def admin_transfer_pick_menu(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show the source user's configs so the admin can move one — or all."""
    from services.admin_transfer import config_label, list_transferable_configs

    await callback.answer()
    source_id = callback_data.user_id
    subs = await list_transferable_configs(session, source_id)
    if not subs:
        await safe_edit_or_send(callback, "این کاربر هیچ کانفیگی برای انتقال ندارد.")
        return

    await state.clear()
    await state.update_data(xfer_source_user_id=str(source_id))

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"📦 انتقال همه ({len(subs)} کانفیگ)",
        callback_data=AdminXferPickCallback(scope="all").pack(),
    )
    for sub in subs[:25]:
        builder.button(
            text=f"🔁 {config_label(sub)}"[:64],
            callback_data=AdminXferPickCallback(scope="one", sub_id=sub.id).pack(),
        )
    builder.button(
        text=AdminButtons.BACK,
        callback_data=AdminUserActionCallback(action="profile", user_id=source_id).pack(),
    )
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        "🔄 <b>انتقال کانفیگ‌ها</b>\n\nکدام کانفیگ را به اکانت دیگری منتقل می‌کنی؟",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(AdminXferPickCallback.filter())
async def admin_transfer_target_prompt(
    callback: CallbackQuery,
    callback_data: AdminXferPickCallback,
    state: FSMContext,
) -> None:
    """After picking scope, ask for the destination account identifier."""
    await callback.answer()
    data = await state.get_data()
    if not data.get("xfer_source_user_id"):
        await safe_edit_or_send(callback, "⌛ نشست انتقال منقضی شد. دوباره از پروفایل کاربر شروع کن.")
        return

    await state.update_data(
        xfer_scope=callback_data.scope,
        xfer_sub_id=(str(callback_data.sub_id) if callback_data.sub_id else None),
    )
    await state.set_state(ManageUserStates.waiting_for_transfer_target)

    scope_text = "همه‌ی کانفیگ‌ها" if callback_data.scope == "all" else "این کانفیگ"
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ لغو", callback_data="admxfer:cancel")
    await safe_edit_or_send(
        callback,
        f"🔄 انتقالِ <b>{scope_text}</b>\n\n"
        "آی‌دی عددی تلگرام یا یوزرنیم (بدون @) اکانتِ <b>مقصد</b> را بفرست.\n\n"
        "⚠️ اکانت مقصد باید عضو ربات باشد.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admxfer:cancel")
async def admin_transfer_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await safe_edit_or_send(callback, "❌ انتقال لغو شد.")


@router.message(ManageUserStates.waiting_for_transfer_target)
async def admin_transfer_target_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Admin entered the destination account; resolve it and ask to confirm."""
    from services.admin_transfer import resolve_target_user

    if not message.text:
        return
    if message.text.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("❌ انتقال لغو شد.")
        return

    data = await state.get_data()
    source_id = data.get("xfer_source_user_id")
    scope = data.get("xfer_scope")
    if not source_id or not scope:
        await state.clear()
        await message.answer("❌ اطلاعات انتقال یافت نشد. دوباره تلاش کن.")
        return

    target = await resolve_target_user(session, message.text)
    if target is None:
        await message.answer("❌ کاربری با این مشخصات پیدا نشد. دوباره بفرست یا /cancel.")
        return
    if str(target.id) == str(source_id):
        await message.answer("❌ کاربر مبدأ و مقصد یکی هستند. یک اکانت دیگر بفرست.")
        return

    await state.update_data(xfer_target_user_id=str(target.id))

    scope_text = "همه‌ی کانفیگ‌ها" if scope == "all" else "این کانفیگ"
    target_display = f"@{target.username}" if target.username else f"ID: <code>{target.telegram_id}</code>"
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید انتقال", callback_data="admxfer:confirm")
    builder.button(text="❌ لغو", callback_data="admxfer:cancel")
    builder.adjust(1)
    await message.answer(
        "🔄 <b>تأیید انتقال</b>\n\n"
        f"📦 موضوع: <b>{scope_text}</b>\n"
        f"👤 مقصد: {target_display} ({_html_escape(target.first_name or '-')})\n\n"
        "ℹ️ لینکِ کانفیگ <u>تغییر نمی‌کند</u>؛ فقط مالکیت به اکانت مقصد منتقل می‌شود.\n\n"
        "تأیید می‌کنی؟",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admxfer:confirm")
async def admin_transfer_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    from services.admin_transfer import AdminTransferError, admin_transfer_configs

    await callback.answer()
    data = await state.get_data()
    await state.clear()

    source_id = data.get("xfer_source_user_id")
    target_id = data.get("xfer_target_user_id")
    scope = data.get("xfer_scope")
    sub_id = data.get("xfer_sub_id")
    if not source_id or not target_id or not scope:
        await safe_edit_or_send(callback, "❌ اطلاعات انتقال یافت نشد.")
        return

    sub_ids = None if scope == "all" else [UUID(str(sub_id))]
    try:
        result = await admin_transfer_configs(
            session,
            source_user_id=UUID(str(source_id)),
            target_user_id=UUID(str(target_id)),
            subscription_ids=sub_ids,
            actor_label=f"bot_admin:{admin_user.telegram_id}",
            actor_user_id=admin_user.id,
        )
    except AdminTransferError as exc:
        await safe_edit_or_send(callback, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("admin transfer failed: %s", exc, exc_info=True)
        await safe_edit_or_send(callback, "❌ خطا در انتقال. لطفاً دوباره تلاش کن.")
        return

    await safe_edit_or_send(
        callback,
        "✅ <b>انتقال موفق</b>\n\n"
        f"<b>{result['count']}</b> کانفیگ به <b>{_html_escape(result['target_name'])}</b> منتقل شد.",
        parse_mode="HTML",
    )

    # Best-effort: tell the new owner.
    try:
        await callback.bot.send_message(
            result["target_telegram_id"],
            f"🎁 <b>{result['count']} کانفیگ</b> توسط پشتیبانی به حساب شما اضافه شد.\n\n"
            "از بخش «📋 سرویس‌های من» می‌توانید مشاهده کنید.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify transfer recipient failed: %s", exc)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_user_profile_text(*, user: User, total_orders: int) -> str:
    wallet_balance = user.wallet.balance if user.wallet is not None else Decimal("0")
    role_text = "ادمین 👑" if user.role == "admin" else ("مالک 💎" if user.role == "owner" else "کاربر عادی 👤")
    verified_phone = get_verified_phone(user)
    phone_text = f"<code>{verified_phone}</code>" if verified_phone else "ثبت نشده"
    discount = getattr(user, "personal_discount_percent", 0) or 0
    discount_text = f"\n🏷 تخفیف شخصی: <b>{discount}٪</b>" if discount > 0 else ""
    return AdminMessages.USER_PROFILE.format(
        # Escape: this text is sent with parse_mode=HTML via plain message.answer
        # in several handlers (no escape fallback). A first_name with < > & would
        # otherwise make the whole profile fail to render.
        name=_html_escape(user.first_name or "-"),
        telegram_id=user.telegram_id,
        status=user.status,
        wallet_balance=f"{wallet_balance:.2f}",
        total_orders=total_orders,
    ) + f"\n🎖 نقش: {role_text}\n📱 شماره موبایل: {phone_text}{discount_text}"


def _build_user_profile_keyboard(user_id: UUID, status: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=AdminButtons.EDIT_BALANCE,
        callback_data=AdminUserActionCallback(action="edit_balance", user_id=user_id).pack(),
    )
    builder.button(
        text="🏷 تخفیف شخصی",
        callback_data=AdminUserActionCallback(action="set_discount", user_id=user_id).pack(),
    )
    builder.button(
        text=AdminButtons.BAN_USER if status != "banned" else AdminButtons.UNBAN_USER,
        callback_data=AdminUserActionCallback(action="toggle_ban", user_id=user_id).pack(),
    )
    builder.button(
        text=AdminButtons.VIEW_CONFIGS,
        callback_data=AdminUserActionCallback(action="view_configs", user_id=user_id).pack(),
    )
    builder.button(
        text="🔄 انتقال کانفیگ‌ها",
        callback_data=AdminUserActionCallback(action="transfer_configs", user_id=user_id).pack(),
    )
    builder.button(
        text="📩 ارسال پیام",
        callback_data=AdminUserActionCallback(action="send_msg", user_id=user_id).pack(),
    )
    builder.button(
        text="👑 تغییر نقش (ادمین/کاربر)",
        callback_data=AdminUserActionCallback(action="toggle_admin", user_id=user_id).pack(),
    )
    builder.button(
        text="🧪 ریست کانفیگ تست",
        callback_data=AdminUserActionCallback(action="reset_trial", user_id=user_id).pack(),
    )
    builder.button(
        text=AdminButtons.BACK,
        callback_data="admin:users",
    )
    builder.adjust(1)
    return builder.as_markup()
