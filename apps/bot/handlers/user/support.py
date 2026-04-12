from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.admin.support import SupportTicketActionCallback
from apps.bot.states.support import UserSupportStates
from core.texts import Buttons, Messages, SupportTexts
from models.user import User
from repositories.ticket import TicketRepository
from repositories.user import UserRepository


router = Router(name="user-support")


@router.message(Command("cancel"))
async def cancel_support_state(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer(Messages.CANCELLED)


@router.message(F.text == Buttons.SUPPORT)
async def support_start(message: Message, state: FSMContext) -> None:
    await state.set_state(UserSupportStates.waiting_for_issue)
    await message.answer(SupportTexts.START)


@router.message(UserSupportStates.waiting_for_issue)
async def support_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot,
) -> None:
    if message.from_user is None or message.text is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer(SupportTexts.ACCOUNT_NOT_FOUND)
        return

    ticket_repository = TicketRepository(session)
    ticket = await ticket_repository.get_open_ticket_for_user(user.id)
    if ticket is None:
        ticket = await ticket_repository.create_ticket(user_id=user.id, status="open")
    else:
        ticket.status = "open"

    await ticket_repository.add_message(ticket_id=ticket.id, sender_id=user.id, text=message.text.strip())
    await state.clear()
    await message.answer(SupportTexts.TICKET_CREATED.format(ticket_id=ticket.id))

    admin_result = await session.execute(
        select(User).where(User.role.in_(["admin", "owner"]), User.status == "active")
    )
    admins = list(admin_result.scalars().all())
    if not admins:
        return

    builder = InlineKeyboardBuilder()
    builder.button(
        text=SupportTexts.ADMIN_REPLY_BUTTON.format(ticket_short=str(ticket.id)[:8]),
        callback_data=SupportTicketActionCallback(action="reply", ticket_id=ticket.id).pack(),
    )
    builder.adjust(1)
    alert_text = SupportTexts.ADMIN_ALERT.format(
        ticket_id=ticket.id,
        name=user.first_name or "-",
        telegram_id=user.telegram_id,
        message=message.text.strip(),
    )
    for admin in admins:
        try:
            await bot.send_message(admin.telegram_id, alert_text, reply_markup=builder.as_markup())
        except (TelegramForbiddenError, TelegramBadRequest):
            admin.is_bot_blocked = True
