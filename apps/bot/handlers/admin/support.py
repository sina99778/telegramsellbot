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
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 بازگشت", callback_data="admin:main")
        builder.adjust(1)
        await safe_edit_or_send(callback, AdminMessages.NO_OPEN_TICKETS, reply_markup=builder.as_markup())
        return

    builder = InlineKeyboardBuilder()
    lines: list[str] = ["📬 <b>تیکت‌های باز پشتیبانی</b>\n"]
    for ticket in tickets:
        user_name = ticket.user.first_name if ticket.user is not None and ticket.user.first_name else "کاربر"
        preview = _build_ticket_preview(ticket)
        status_icon = "🟢" if ticket.status == "open" else "🟡"
        lines.append(
            f"{status_icon} <b>#{str(ticket.id)[:8]}</b> | {user_name} | {_format_ticket_status(ticket.status)}\n"
            f"   └ {preview}"
        )
        builder.button(
            text=f"{status_icon} {user_name} | {_format_ticket_status(ticket.status)}",
            callback_data=SupportTicketActionCallback(action="view", ticket_id=ticket.id).pack(),
        )
    builder.button(text="🔙 بازگشت به پنل مدیریت", callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(callback, "\n\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")


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

    recent_messages = ticket.messages[-10:]  # Show more messages for admin
    msg_lines = []
    for message in recent_messages:
        sender = "🛠 ادمین" if message.sender_id != ticket.user_id else "👤 کاربر"
        content = message.text or SupportTexts.PHOTO_MARKER
        msg_lines.append(f"{sender}: {content}")
    rendered_messages = "\n\n".join(msg_lines) or "-"

    user_link = (
        f"@{ticket.user.username}" if ticket.user.username
        else f"<a href='tg://user?id={ticket.user.telegram_id}'>مشاهده پروفایل</a>"
    )

    text = (
        f"🎫 <b>جزئیات تیکت #{str(ticket.id)[:8]}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: {ticket.user.first_name or '-'} | {user_link}\n"
        f"🆔 آیدی تلگرام: <code>{ticket.user.telegram_id}</code>\n"
        f"📊 وضعیت: {_format_ticket_status(ticket.status)}\n"
        f"📨 تعداد پیام‌ها: {len(ticket.messages)}\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📝 <b>آخرین پیام‌ها:</b>\n\n"
        f"{rendered_messages}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💬 پاسخ دادن",
        callback_data=SupportTicketActionCallback(action="reply", ticket_id=ticket.id).pack(),
    )
    if ticket.status != "closed":
        builder.button(
            text="🔒 بستن تیکت",
            callback_data=SupportTicketActionCallback(action="close", ticket_id=ticket.id).pack(),
        )
    builder.button(
        text="🔙 لیست تیکت‌ها",
        callback_data="admin:tickets",
    )
    builder.adjust(2, 1)

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(SupportTicketActionCallback.filter(F.action == "reply"))
async def support_reply_start(
    callback: CallbackQuery,
    callback_data: SupportTicketActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(SupportReplyStates.waiting_for_reply)
    await state.update_data(
        ticket_id=str(callback_data.ticket_id),
        prompt_chat_id=callback.message.chat.id if callback.message else None,
        prompt_message_id=callback.message.message_id if callback.message else None,
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ انصراف", callback_data="admin:support:cancel_reply")
    builder.adjust(1)

    await safe_edit_or_send(
        callback,
        f"💬 پاسخ تیکت #{str(callback_data.ticket_id)[:8]}\n\n"
        f"{SupportTexts.ADMIN_REPLY_PROMPT}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:support:cancel_reply")
async def support_cancel_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()

    # Go back to ticket view if possible
    raw_ticket_id = data.get("ticket_id")
    if raw_ticket_id:
        builder = InlineKeyboardBuilder()
        builder.button(
            text="📋 بازگشت به تیکت",
            callback_data=SupportTicketActionCallback(
                action="view", ticket_id=UUID(str(raw_ticket_id))
            ).pack(),
        )
        builder.button(text="🔙 لیست تیکت‌ها", callback_data="admin:tickets")
        builder.adjust(1)
        await safe_edit_or_send(callback, "❌ پاسخ لغو شد.", reply_markup=builder.as_markup())
    else:
        await safe_edit_or_send(callback, "❌ پاسخ لغو شد.")


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
        user_reply_text = (
            f"💬 <b>پاسخ پشتیبانی</b> — تیکت #{str(ticket.id)[:8]}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"{clean_text or SupportTexts.PHOTO_MARKER}"
        )
        
        # Build user reply keyboard with reply + close options
        user_builder = InlineKeyboardBuilder()
        user_builder.button(
            text="💬 پاسخ دادن",
            callback_data=SupportTicketActionCallback(action="user_reply", ticket_id=ticket.id).pack(),
        )
        user_builder.button(
            text="🔒 بستن تیکت",
            callback_data=SupportTicketActionCallback(action="user_close", ticket_id=ticket.id).pack(),
        )
        user_builder.adjust(2)
        
        if photo_id:
            await bot.send_photo(
                chat_id=ticket.user.telegram_id,
                photo=photo_id,
                caption=user_reply_text,
                reply_markup=user_builder.as_markup(),
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=ticket.user.telegram_id,
                text=user_reply_text,
                reply_markup=user_builder.as_markup(),
                parse_mode="HTML",
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

    # Send confirmation with quick action buttons
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📋 مشاهده تیکت",
        callback_data=SupportTicketActionCallback(action="view", ticket_id=ticket.id).pack(),
    )
    builder.button(
        text="🔒 بستن تیکت",
        callback_data=SupportTicketActionCallback(action="close", ticket_id=ticket.id).pack(),
    )
    builder.button(text="📬 لیست تیکت‌ها", callback_data="admin:tickets")
    builder.adjust(2, 1)

    # Try to edit the prompt message
    prompt_chat_id = state_data.get("prompt_chat_id")
    prompt_message_id = state_data.get("prompt_message_id")
    if prompt_chat_id and prompt_message_id:
        try:
            await bot.edit_message_text(
                chat_id=int(prompt_chat_id),
                message_id=int(prompt_message_id),
                text=f"✅ {SupportTexts.ADMIN_REPLY_SENT}\n\nتیکت: #{str(ticket.id)[:8]}",
                reply_markup=builder.as_markup(),
            )
            return
        except Exception:
            pass
    await message.answer(
        f"✅ {SupportTexts.ADMIN_REPLY_SENT}",
        reply_markup=builder.as_markup(),
    )


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

    builder = InlineKeyboardBuilder()
    builder.button(text="📬 لیست تیکت‌ها", callback_data="admin:tickets")
    builder.button(text="🔙 پنل مدیریت", callback_data="admin:main")
    builder.adjust(1)

    await safe_edit_or_send(
        callback,
        f"✅ تیکت #{str(ticket.id)[:8]} با موفقیت بسته شد.",
        reply_markup=builder.as_markup(),
    )


def _build_close_ticket_keyboard(ticket_id: UUID):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💬 پاسخ دادن",
        callback_data=SupportTicketActionCallback(action="user_reply", ticket_id=ticket_id).pack(),
    )
    builder.button(
        text=SupportTexts.CLOSE_TICKET,
        callback_data=SupportTicketActionCallback(action="user_close", ticket_id=ticket_id).pack(),
    )
    builder.adjust(2)
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
        "open": "🟢 باز",
        "answered": "🟡 پاسخ داده شده",
        "closed": "🔴 بسته",
    }.get(status, status)
