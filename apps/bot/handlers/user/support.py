from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.admin.support import SupportTicketActionCallback
from apps.bot.states.support import UserSupportStates
from core.config import settings
from models.user import User
from repositories.ticket import TicketRepository
from repositories.user import UserRepository


router = Router(name="user-support")


@router.message(Command("cancel"))
async def cancel_support_state(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Cancelled.")


@router.message(F.text == "🛠 Support")
async def support_start(message: Message, state: FSMContext) -> None:
    await state.set_state(UserSupportStates.waiting_for_issue)
    await message.answer("Describe your issue and support will get back to you here. Send /cancel to stop.")


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
        await message.answer("Your account was not found. Please use /start first.")
        return

    ticket_repository = TicketRepository(session)
    ticket = await ticket_repository.get_open_ticket_for_user(user.id)
    if ticket is None:
        ticket = await ticket_repository.create_ticket(user_id=user.id, status="open")
    else:
        ticket.status = "open"

    await ticket_repository.add_message(ticket_id=ticket.id, sender_id=user.id, text=message.text.strip())
    await state.clear()
    await message.answer(f"Your message has been sent to support. Ticket ID: {ticket.id}")

    admin_result = await session.execute(
        select(User).where(User.role.in_(["admin", "owner"]), User.status == "active")
    )
    admins = list(admin_result.scalars().all())
    if not admins:
        return

    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💬 Reply (Ticket #{str(ticket.id)[:8]})",
        callback_data=SupportTicketActionCallback(action="reply", ticket_id=ticket.id).pack(),
    )
    builder.adjust(1)
    alert_text = (
        "New support ticket\n\n"
        f"Ticket: {ticket.id}\n"
        f"User: {user.first_name or '-'}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Message: {message.text.strip()}"
    )
    for admin in admins:
        await bot.send_message(admin.telegram_id, alert_text, reply_markup=builder.as_markup())
