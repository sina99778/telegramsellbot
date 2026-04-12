from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import ManageUserStates
from models.order import Order
from models.subscription import Subscription
from models.user import User
from repositories.audit import AuditLogRepository
from repositories.user import UserRepository
from services.wallet.manager import WalletManager


router = Router(name="admin-users")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class AdminUserActionCallback(CallbackData, prefix="admin_user"):
    action: str
    user_id: UUID


@router.callback_query(F.data == "admin:users")
async def admin_users_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_telegram_id)
    await callback.message.answer("Send the user's Telegram ID.")


@router.message(ManageUserStates.waiting_for_telegram_id)
async def admin_users_lookup(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return

    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("Telegram ID must be a valid integer.")
        return

    user = await session.scalar(
        select(User)
        .options(
            selectinload(User.wallet),
            selectinload(User.subscriptions),
        )
        .where(User.telegram_id == telegram_id)
    )
    if user is None or user.wallet is None:
        await message.answer("User not found.")
        return

    total_orders = int(
        await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user.id)) or 0
    )
    await state.clear()
    await message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
    )


@router.callback_query(AdminUserActionCallback.filter(F.action == "edit_balance"))
async def admin_edit_balance_prompt(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(ManageUserStates.waiting_for_balance_adjustment)
    await state.update_data(target_user_id=str(callback_data.user_id))
    await callback.message.answer("Enter a positive amount to add or a negative amount to deduct.")


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
        await message.answer("No target user is selected.")
        return

    try:
        amount = Decimal(message.text.strip())
    except InvalidOperation:
        await message.answer("Please enter a valid decimal amount.")
        return

    if amount == Decimal("0"):
        await message.answer("Amount cannot be zero.")
        return

    target_user_id = UUID(str(raw_user_id))
    user = await session.scalar(select(User).options(selectinload(User.wallet)).where(User.id == target_user_id))
    if user is None or user.wallet is None:
        await state.clear()
        await message.answer("User not found.")
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
    await state.clear()
    await message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
    )


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
        await callback.message.answer("User not found.")
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
    await callback.message.answer(
        _build_user_profile_text(user=user, total_orders=total_orders),
        reply_markup=_build_user_profile_keyboard(user.id, user.status),
    )


def _build_user_profile_text(*, user: User, total_orders: int) -> str:
    wallet_balance = user.wallet.balance if user.wallet is not None else Decimal("0")
    return (
        f"User: {user.first_name or '-'}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Status: {user.status}\n"
        f"Wallet Balance: {wallet_balance} USD\n"
        f"Total Orders: {total_orders}"
    )


def _build_user_profile_keyboard(user_id: UUID, status: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💰 Edit Balance",
        callback_data=AdminUserActionCallback(action="edit_balance", user_id=user_id).pack(),
    )
    builder.button(
        text="🚫 Ban User" if status != "banned" else "✅ Unban User",
        callback_data=AdminUserActionCallback(action="toggle_ban", user_id=user_id).pack(),
    )
    builder.button(
        text="📋 View User Configs",
        callback_data=AdminUserActionCallback(action="view_configs", user_id=user_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()
