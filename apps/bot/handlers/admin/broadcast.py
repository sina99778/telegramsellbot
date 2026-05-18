from __future__ import annotations

import html as _html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import BroadcastStates
from core.texts import AdminMessages
from models.broadcast import BroadcastJob
from models.user import User
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-broadcast")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BroadcastStates.waiting_for_message)
    await safe_edit_or_send(callback, AdminMessages.BROADCAST_START)


@router.message(BroadcastStates.waiting_for_message)
async def broadcast_capture(message: Message, state: FSMContext, session: AsyncSession) -> None:
    payload: dict[str, str | None] = {
        "message_type": "text",
        "text": message.text,
        "media_file_id": None,
        "media_caption": None,
    }

    if message.photo:
        payload["message_type"] = "photo"
        payload["media_file_id"] = message.photo[-1].file_id
        payload["media_caption"] = message.caption
        payload["text"] = None
    elif message.text is None:
        await message.answer(AdminMessages.BROADCAST_UNSUPPORTED)
        return

    await state.update_data(broadcast_payload=payload)
    await state.set_state(BroadcastStates.waiting_for_confirmation)

    # Compute the recipient count up-front so the admin doesn't blind-fire.
    recipient_count = int(
        await session.scalar(
            select(func.count()).select_from(User).where(
                User.status == "active",
                User.is_bot_blocked.is_(False),
            )
        ) or 0
    )
    preview = (
        payload.get("text") or payload.get("media_caption") or "(بدون متن)"
    )
    preview_clip = preview if len(preview) <= 200 else preview[:200] + "…"
    # Escape — admin-controlled but `</blockquote>` in the middle of the
    # template would break the parser and could let an admin paste content
    # that renders as something else.
    preview_html = _html.escape(preview_clip)
    await message.answer(
        "📢 <b>پیش‌نمایش پیام همگانی</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 تعداد گیرنده‌ها: <b>{recipient_count:,}</b> کاربر فعال\n"
        f"📝 نوع پیام: <b>{payload['message_type']}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<i>محتوای پیام:</i>\n"
        f"<blockquote>{preview_html}</blockquote>\n\n"
        + AdminMessages.BROADCAST_CONFIRM
    )


@router.message(BroadcastStates.waiting_for_confirmation)
async def broadcast_confirm(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    decision = message.text.strip().lower()
    if decision == "cancel":
        await state.clear()
        await message.answer(AdminMessages.BROADCAST_CANCELLED)
        return
    if decision != "confirm":
        await message.answer(AdminMessages.BROADCAST_CONFIRM_HINT)
        return

    state_data = await state.get_data()
    payload = dict(state_data.get("broadcast_payload", {}))
    broadcast_job = BroadcastJob(
        created_by_user_id=admin_user.id,
        status="queued",
        message_type=str(payload.get("message_type") or "text"),
        text=payload.get("text"),
        media_file_id=payload.get("media_file_id"),
        media_caption=payload.get("media_caption"),
        payload=payload,
    )
    session.add(broadcast_job)
    await session.flush()

    await state.clear()
    await message.answer(AdminMessages.BROADCAST_QUEUED.format(job_id=broadcast_job.id))
