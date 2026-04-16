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
from core.texts import AdminMessages, Messages, SupportTexts
from models.ticket import Ticket
from models.user import User
from repositories.audit import AuditLogRepository
from repositories.ticket import TicketRepository
from apps.bot.utils.messaging import safe_edit_or_send


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


@router.callback_query(F.data == "admin:tickets")
async def support_ticket_list(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    tickets = await TicketRepository(session).list_open_tickets(limit=20)
    if not tickets:
        await safe_edit_or_send(callback, AdminMessages.NO_OPEN_TICKETS)
        return

    builder = InlineKeyboardBuilder()
    lines: list[str] = [AdminMessages.TICKETS_OVERVIEW]
    for ticket in tickets:
        user_name = ticket.user.first_name if ticket.user is not None and ticket.user.first_name else "کاربر"
        preview = _build_ticket_preview(ticket)
        lines.append(
            f"#{str(ticket.id)[:8]} | {user_name} | {_format_ticket_status(ticket.status)}\n"
            f"{preview}"
        )
        builder.button(
            text=f"{user_name} | {_format_ticket_status(ticket.status)}",
            callback_data=SupportTicketActionCallback(action="view", ticket_id=ticket.id).pack(),
        )
    builder.button(text="🔙 بازگشت", callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(callback, "\n\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(SupportTicketActionCallback.filter(F.action == "view"))
async def support_ticket_view(
    callback: CallbackQuery,
    callback_data: SupportTicketActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    ticket = await TicketRepository(session).get_ticket_with_messages(callback_data.ticket_id)
    if ticket is None or ticket.user is None:
        await safe_edit_or_send(callback, SupportTexts.ADMIN_TICKET_NOT_FOUND)
        return

    recent_messages = ticket.messages[-5:]
    rendered_messages = "\n\n".join(
        f"{'ادمین' if message.sender_id != ticket.user_id else 'کاربر'}: {message.text or SupportTexts.PHOTO_MARKER}"
        for message in recent_messages
    ) or "-"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=SupportTexts.ADMIN_REPLY_BUTTON.format(ticket_short=str(ticket.id)[:8]),
        callback_data=SupportTicketActionCallback(action="reply", ticket_id=ticket.id).pack(),
    )
    builder.button(
        text=SupportTexts.CLOSE_TICKET,
        callback_data=SupportTicketActionCallback(action="close", ticket_id=ticket.id).pack(),
    )
    builder.adjust(1)

    await safe_edit_or_send(callback, 
        AdminMessages.TICKET_DETAILS.format(
            ticket_id=ticket.id,
            user_name=ticket.user.first_name or ticket.user.username or "کاربر",
            telegram_id=ticket.user.telegram_id,
            status=_format_ticket_status(ticket.status),
            messages=rendered_messages,
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(SupportTicketActionCallback.filter(F.action == "reply"))
async def support_reply_start(
    callback: CallbackQuery,
    callback_data: SupportTicketActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SupportReplyStates.waiting_for_reply)
    await state.update_data(ticket_id=str(callback_data.ticket_id))
    await safe_edit_or_send(callback, SupportTexts.ADMIN_REPLY_PROMPT)


@router.message(SupportReplyStates.waiting_for_reply)
async def support_reply_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
    bot: Bot,
) -> None:
    if message.text is None and message.photo is None:
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

    clean_text = message.text.strip() if message.text else message.caption.strip() if message.caption else None
    photo_id = message.photo[-1].file_id if message.photo else None
    
    await ticket_repository.add_message(
        ticket_id=ticket.id,
        sender_id=admin_user.id,
        text=clean_text,
        photo_id=photo_id
    )

    try:
        user_reply_text = SupportTexts.USER_REPLY.format(
            ticket_id=ticket.id,
            message=clean_text or SupportTexts.PHOTO_MARKER
        )
        if photo_id:
            await bot.send_photo(
                chat_id=ticket.user.telegram_id,
                photo=photo_id,
                caption=user_reply_text,
                reply_markup=_build_close_ticket_keyboard(ticket.id),
            )
        else:
            await bot.send_message(
                chat_id=ticket.user.telegram_id,
                text=user_reply_text,
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
        await safe_edit_or_send(callback, SupportTexts.ADMIN_TICKET_NOT_FOUND)
        return

    await ticket_repository.set_status(ticket, "closed")
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="close_ticket",
        entity_type="ticket",
        entity_id=ticket.id,
        payload={"status": "closed"},
    )
    await safe_edit_or_send(callback, SupportTexts.TICKET_CLOSED.format(ticket_id=ticket.id))


def _build_close_ticket_keyboard(ticket_id: UUID):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=SupportTexts.CLOSE_TICKET,
        callback_data=SupportTicketActionCallback(action="close", ticket_id=ticket_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()

def _build_ticket_preview(ticket: Ticket) -> str:
    if not ticket.messages:
        return "-"
    last_msg = ticket.messages[-1]
    msg_content = last_msg.text or SupportTexts.PHOTO_MARKER
    preview = msg_content.replace("\n", " ").strip()
    if len(preview) > 44:
        return preview[:41].rstrip() + "..."
    return preview


def _format_ticket_status(status: str) -> str:
    return {
        "open": "باز",
        "answered": "پاسخ داده شده",
        "closed": "بسته",
    }.get(status, status)
