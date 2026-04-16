from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import RetargetingStates
from core.texts import AdminButtons, AdminMessages, Common
from models.user import User
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-retargeting")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class RetargetingActionCallback(CallbackData, prefix="retargeting"):
    action: str


@router.message(Command("cancel"), RetargetingStates.waiting_for_message)
@router.message(Command("cancel"), RetargetingStates.waiting_for_days)
async def cancel_retargeting_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("عملیات لغو شد.")


@router.callback_query(F.data == "admin:retargeting")
async def retargeting_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await safe_edit_or_send(callback, 
        _format_retargeting_menu(await AppSettingsRepository(session).get_retargeting_settings()),
        reply_markup=_build_retargeting_keyboard(),
    )


@router.callback_query(RetargetingActionCallback.filter(F.action == "toggle"))
async def toggle_retargeting(
    callback: CallbackQuery,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    repository = AppSettingsRepository(session)
    current = await repository.get_retargeting_settings()
    updated = await repository.update_retargeting_settings(enabled=not current.enabled)
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="update_retargeting_enabled",
        entity_type="app_setting",
        entity_id=None,
        payload={"from": current.enabled, "to": updated.enabled},
    )
    await callback.message.edit_text(
        _format_retargeting_menu(updated),
        reply_markup=_build_retargeting_keyboard(),
    )


@router.callback_query(RetargetingActionCallback.filter(F.action == "edit_text"))
async def prompt_retargeting_text(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(RetargetingStates.waiting_for_message)
    await safe_edit_or_send(callback, AdminMessages.RETARGETING_ENTER_MESSAGE)


@router.message(RetargetingStates.waiting_for_message)
async def save_retargeting_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    updated = await AppSettingsRepository(session).update_retargeting_settings(message=message.text)
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="update_retargeting_message",
        entity_type="app_setting",
        entity_id=None,
        payload={"message": updated.message},
    )
    await state.clear()
    await message.answer(AdminMessages.RETARGETING_UPDATED)
    await message.answer(_format_retargeting_menu(updated), reply_markup=_build_retargeting_keyboard())


@router.callback_query(RetargetingActionCallback.filter(F.action == "edit_days"))
async def prompt_retargeting_days(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(RetargetingStates.waiting_for_days)
    await safe_edit_or_send(callback, AdminMessages.RETARGETING_ENTER_DAYS)


@router.message(RetargetingStates.waiting_for_days)
async def save_retargeting_days(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    try:
        days = int(message.text.strip())
    except ValueError:
        await message.answer(AdminMessages.INVALID_INTEGER)
        return

    if days <= 0:
        await message.answer(AdminMessages.DURATION_GT_ZERO)
        return

    updated = await AppSettingsRepository(session).update_retargeting_settings(days=days)
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="update_retargeting_days",
        entity_type="app_setting",
        entity_id=None,
        payload={"days": updated.days},
    )
    await state.clear()
    await message.answer(AdminMessages.RETARGETING_UPDATED)
    await message.answer(_format_retargeting_menu(updated), reply_markup=_build_retargeting_keyboard())


@router.callback_query(RetargetingActionCallback.filter(F.action == "test"))
async def test_retargeting_message(
    callback: CallbackQuery,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    settings = await AppSettingsRepository(session).get_retargeting_settings()
    await callback.bot.send_message(admin_user.telegram_id, settings.message)
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="test_retargeting_message",
        entity_type="app_setting",
        entity_id=None,
        payload={"days": settings.days, "enabled": settings.enabled},
    )
    await safe_edit_or_send(callback, AdminMessages.RETARGETING_TEST_SENT)


def _build_retargeting_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(
        text=AdminButtons.TOGGLE_RETARGETING,
        callback_data=RetargetingActionCallback(action="toggle").pack(),
    )
    builder.button(
        text=AdminButtons.EDIT_RETARGETING_TEXT,
        callback_data=RetargetingActionCallback(action="edit_text").pack(),
    )
    builder.button(
        text=AdminButtons.EDIT_RETARGETING_DAYS,
        callback_data=RetargetingActionCallback(action="edit_days").pack(),
    )
    builder.button(
        text=AdminButtons.TEST_RETARGETING,
        callback_data=RetargetingActionCallback(action="test").pack(),
    )
    builder.button(
        text=AdminButtons.BACK,
        callback_data="admin:main",
    )
    builder.adjust(1)
    return builder.as_markup()


def _format_retargeting_menu(settings) -> str:
    return AdminMessages.RETARGETING_MENU.format(
        status=Common.ACTIVE if settings.enabled else Common.INACTIVE,
        days=settings.days,
        message=settings.message,
    )
