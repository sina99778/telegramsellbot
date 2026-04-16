from __future__ import annotations

from decimal import Decimal, InvalidOperation
from math import ceil
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import ManageUserStates
from core.texts import AdminButtons, AdminMessages
from models.order import Order
from models.subscription import Subscription
from models.user import User
from repositories.audit import AuditLogRepository
from repositories.user import UserRepository
from services.wallet.manager import WalletManager
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-users")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

USER_PAGE_SIZE = 10


class AdminUserActionCallback(CallbackData, prefix="admin_user"):
    action: str
    user_id: UUID


class AdminUserListPageCallback(CallbackData, prefix="admin_userlist"):
    page: int


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

    # Layout: user buttons 1 per row, then pagination row, then back
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

    # Try numeric telegram_id first, fallback to username search
    user: User | None = None
    try:
        telegram_id = int(query_text)
        user = await session.scalar(
            select(User)
            .options(selectinload(User.wallet), selectinload(User.subscriptions))
            .where(User.telegram_id == telegram_id)
        )
    except ValueError:
        # Search by username (case-insensitive)
        user = await session.scalar(
            select(User)
            .options(selectinload(User.wallet), selectinload(User.subscriptions))
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
        .options(selectinload(User.wallet), selectinload(User.subscriptions))
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
    user = await session.scalar(select(User).options(selectinload(User.wallet)).where(User.id == target_user_id))
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
    user = await UserRepository(session).get(callback_data.user_id)
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    if user.status == "banned":
        user.status = "active"
        user.is_bot_blocked = False
    else:
        user.status = "banned"
        user.is_bot_blocked = True

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_ban",
        entity_type="user",
        entity_id=user.id,
        payload={"status": user.status, "is_bot_blocked": user.is_bot_blocked},
    )

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await safe_edit_or_send(callback, 
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
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

    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
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


@router.callback_query(AdminUserActionCallback.filter(F.action == "toggle_admin"))
async def admin_user_toggle_admin(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet), selectinload(User.subscriptions))
        .where(User.id == callback_data.user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return
        
    # Toggle logic
    from core.config import settings
    # Cannot demote owner
    if user.telegram_id == settings.owner_telegram_id:
        await safe_edit_or_send(callback, "❌ نقش مالک اصلی ربات (owner) قابل تغییر نیست.")
        return

    new_role = "admin" if user.role == "user" else "user"
    user.role = new_role
    await session.flush()
    
    # Reload profile page with updated role
    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    status_text = "ادمین 👑" if new_role == "admin" else "کاربر عادی 👤"
    if callback.message:
        try:
            await callback.message.edit_text(
                _build_user_profile_text(user=user, total_orders=total_orders) + f"\n\n✅ نقش به {status_text} تغییر یافت.",
                reply_markup=_build_user_profile_keyboard(user.id, user.status),
            )
        except Exception:
            await safe_edit_or_send(callback, 
                _build_user_profile_text(user=user, total_orders=total_orders) + f"\n\n✅ نقش به {status_text} تغییر یافت.",
                reply_markup=_build_user_profile_keyboard(user.id, user.status),
            )


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_user_profile_text(*, user: User, total_orders: int) -> str:
    wallet_balance = user.wallet.balance if user.wallet is not None else Decimal("0")
    role_text = "ادمین 👑" if user.role == "admin" else ("مالک 💎" if user.role == "owner" else "کاربر عادی 👤")
    return AdminMessages.USER_PROFILE.format(
        name=user.first_name or "-",
        telegram_id=user.telegram_id,
        status=user.status,
        wallet_balance=f"{wallet_balance:.2f}",
        total_orders=total_orders,
    ) + f"\n🎖 نقش: {role_text}"


def _build_user_profile_keyboard(user_id: UUID, status: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=AdminButtons.EDIT_BALANCE,
        callback_data=AdminUserActionCallback(action="edit_balance", user_id=user_id).pack(),
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
        text="📩 ارسال پیام",
        callback_data=AdminUserActionCallback(action="send_msg", user_id=user_id).pack(),
    )
    # Add toggle admin role button
    # To determine text, we need the user's current role which isn't passed here.
    # We should update _build_user_profile_keyboard signature if we want dynamic text. 
    # For now, we will add a generic "تغییر نقش (ادمین/کاربر)" button.
    builder.button(
        text="👑 تغییر نقش (ادمین/کاربر)",
        callback_data=AdminUserActionCallback(action="toggle_admin", user_id=user_id).pack(),
    )
    builder.button(
        text=AdminButtons.BACK,
        callback_data="admin:users",
    )
    builder.adjust(1)
    return builder.as_markup()

