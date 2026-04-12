from __future__ import annotations

from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import SupportReplyStates
from core.texts import Messages, SupportTexts
from models.ticket import Ticket
from models.user import User
from repositories.audit import AuditLogRepository
from repositories.ticket import TicketRepository


router = Router(name="admin-support")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class SupportTicketActionCallback(CallbackData, prefix="support"):
    action: str
    ticket_id: UUID


@router.message(Command("cancel"))
async def cancel_admin_support_state(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer(Messages.CANCELLED)


@router.callback_query(SupportTicketActionCallback.filter(F.action == "reply"))
async def support_reply_start(
    callback: CallbackQuery,
    callback_data: SupportTicketActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SupportReplyStates.waiting_for_reply)
    await state.update_data(ticket_id=str(callback_data.ticket_id))
    await callback.message.answer(SupportTexts.ADMIN_REPLY_PROMPT)


@router.message(SupportReplyStates.waiting_for_reply)
async def support_reply_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
    bot: Bot,
) -> None:
    if message.text is None:
        return

    state_data = await state.get_data()
    raw_ticket_id = state_data.get("ticket_id")
    if raw_ticket_id is None:
        await state.clear()
        await message.answer(SupportTexts.ADMIN_NO_TICKET)
        return

    ticket_repository = TicketRepository(session)
    ticket = await session.scalar(
        select(Ticket)
        .options(selectinload(Ticket.user))
        .where(Ticket.id == UUID(str(raw_ticket_id)))
    )
    if ticket is None or ticket.user is None:
        await state.clear()
        await message.answer(SupportTexts.ADMIN_TICKET_NOT_FOUND)
        return

    await ticket_repository.add_message(ticket_id=ticket.id, sender_id=admin_user.id, text=message.text.strip())

    try:
        await bot.send_message(
            chat_id=ticket.user.telegram_id,
            text=SupportTexts.USER_REPLY.format(ticket_id=ticket.id, message=message.text.strip()),
            reply_markup=_build_close_ticket_keyboard(ticket.id),
        )
        ticket.status = "answered"
    except TelegramForbiddenError:
        ticket.status = "closed"
        ticket.user.is_bot_blocked = True
        await message.answer(SupportTexts.ADMIN_USER_BLOCKED)
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="reply_ticket_blocked",
            entity_type="ticket",
            entity_id=ticket.id,
            payload={"user_id": str(ticket.user_id)},
        )
        await state.clear()
        return

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="reply_ticket",
        entity_type="ticket",
        entity_id=ticket.id,
        payload={"user_id": str(ticket.user_id), "status": ticket.status},
    )
    await state.clear()
    await message.answer(SupportTexts.ADMIN_REPLY_SENT)


@router.callback_query(SupportTicketActionCallback.filter(F.action == "close"))
async def support_close_ticket(
    callback: CallbackQuery,
    callback_data: SupportTicketActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    ticket_repository = TicketRepository(session)
    ticket = await ticket_repository.get(callback_data.ticket_id)
    if ticket is None:
        await callback.message.answer(SupportTexts.ADMIN_TICKET_NOT_FOUND)
        return

    await ticket_repository.set_status(ticket, "closed")
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="close_ticket",
        entity_type="ticket",
        entity_id=ticket.id,
        payload={"status": "closed"},
    )
    await callback.message.answer(SupportTexts.TICKET_CLOSED.format(ticket_id=ticket.id))


def _build_close_ticket_keyboard(ticket_id: UUID):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=SupportTexts.CLOSE_TICKET,
        callback_data=SupportTicketActionCallback(action="close", ticket_id=ticket_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()
