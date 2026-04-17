from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
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
async def support_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.from_user is None:
        return
        
    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer(SupportTexts.ACCOUNT_NOT_FOUND)
        return

    ticket_repository = TicketRepository(session)
    ticket = await ticket_repository.get_open_ticket_for_user(user.id)
    
    await state.set_state(UserSupportStates.waiting_for_issue)
    
    from aiogram.types import InlineKeyboardMarkup
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ انصراف و خروج از پشتیبانی", callback_data="support:cancel")
    reply_markup = builder.as_markup()

    if ticket:
        messages = ticket.messages[-5:] # Last 5 messages
        history_text = SupportTexts.HISTORY_TITLE.format(ticket_id=str(ticket.id)[:8])
        for msg in messages:
            sender = "شما" if msg.sender_id == user.id else "پشتیبانی"
            content = msg.text or SupportTexts.PHOTO_MARKER
            history_text += f"🔹 {sender}: {content}\n"
        
        history_text += f"\n{SupportTexts.START}"
        await message.answer(history_text, reply_markup=reply_markup)
    else:
        await message.answer(SupportTexts.START, reply_markup=reply_markup)


@router.callback_query(F.data == "support:cancel")
async def support_cancel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await state.get_state() is not None:
        await state.clear()
    
    # Try to clean up the message
    try:
        await callback.message.delete()
    except Exception:
        pass
    
    await callback.message.answer(Messages.CANCELLED)


@router.message(UserSupportStates.waiting_for_issue)
async def support_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    if message.from_user is None:
        return

    # Accept text OR photo
    if not message.text and not message.photo:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer(SupportTexts.ACCOUNT_NOT_FOUND)
        return

    ticket_repository = TicketRepository(session)
    ticket = await ticket_repository.get_open_ticket_for_user(user.id)
    
    is_new_ticket = False
    if ticket is None:
        ticket = await ticket_repository.create_ticket(user_id=user.id, status="open")
        is_new_ticket = True
    else:
        if ticket.status == "answered":
            ticket.status = "open"

    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.text or message.caption
    
    await ticket_repository.add_message(
        ticket_id=ticket.id,
        sender_id=user.id,
        text=text.strip() if text else None,
        photo_id=photo_id
    )
    
    # We do NOT clear the state anymore, so user stays in conversation mode.
    # However, we send a confirmation for the first message only or a small reaction?
    # User requested history, so they can keep typing.
    
    if is_new_ticket:
        await message.answer(SupportTexts.TICKET_CREATED.format(ticket_id=ticket.id))
    else:
        # Just a small acknowledgement or nothing? Telegram "delivered" is usually enough but bot can't show that.
        # Let's send a small text or just let it be. 
        # Actually, let's just confirm receipt to be safe.
        await message.answer("✅ پیام شما ثبت شد و برای پشتیبان ارسال گردید.")

    # Alert admins
    admin_result = await session.execute(
        select(User).where(User.role.in_(["admin", "owner"]), User.status == "active")
    )
    admins = list(admin_result.scalars().all())
    
    builder = InlineKeyboardBuilder()
    builder.button(
        text=SupportTexts.ADMIN_REPLY_BUTTON.format(ticket_short=str(ticket.id)[:8]),
        callback_data=SupportTicketActionCallback(action="reply", ticket_id=ticket.id).pack(),
    )
    builder.adjust(1)
    
    display_content = text or SupportTexts.PHOTO_MARKER
    alert_text = SupportTexts.ADMIN_ALERT.format(
        ticket_id=ticket.id,
        name=user.first_name or "-",
        telegram_id=user.telegram_id,
        message=display_content.strip() if display_content else "-",
    )
    
    for admin in admins:
        try:
            if photo_id:
                await bot.send_photo(
                    admin.telegram_id,
                    photo_id,
                    caption=alert_text,
                    reply_markup=builder.as_markup()
                )
            else:
                await bot.send_message(
                    admin.telegram_id,
                    alert_text,
                    reply_markup=builder.as_markup()
                )
        except (TelegramForbiddenError, TelegramBadRequest):
            admin.is_bot_blocked = True

