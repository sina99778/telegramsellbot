from __future__ import annotations

from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.handlers.admin.users import AdminUserActionCallback
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from core.texts import AdminButtons, AdminMessages
from models.subscription import Subscription
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.audit import AuditLogRepository
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-subs")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

SUB_PAGE_SIZE = 5


class AdminSubscriptionActionCallback(CallbackData, prefix="admin_sub"):
    action: str
    subscription_id: UUID
    user_id: UUID
    page: int = 1


class AdminSubscriptionListPageCallback(CallbackData, prefix="admin_sub_list"):
    user_id: UUID
    page: int = 1


@router.callback_query(AdminUserActionCallback.filter(F.action == "view_configs"))
async def view_user_configs(
    callback: CallbackQuery,
    callback_data: AdminUserActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=1,
    )


@router.callback_query(AdminSubscriptionListPageCallback.filter())
async def view_user_configs_page(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=callback_data.page,
    )


async def _render_user_configs(
    *,
    callback: CallbackQuery,
    session: AsyncSession,
    user_id: UUID,
    page: int,
) -> None:
    user = await session.scalar(
        select(User)
        .options(
            selectinload(User.subscriptions).selectinload(Subscription.xui_client).selectinload(XUIClientRecord.inbound)
        )
        .where(User.id == user_id)
    )
    if user is None:
        await safe_edit_or_send(callback, AdminMessages.USER_NOT_FOUND)
        return

    active_subs = [sub for sub in user.subscriptions if sub.status in {"pending_activation", "active"}]
    if not active_subs:
        try:
            await callback.message.edit_text(AdminMessages.NO_ACTIVE_CONFIGS)
        except TelegramBadRequest:
            await safe_edit_or_send(callback, AdminMessages.NO_ACTIVE_CONFIGS)
        return

    start = max(page - 1, 0) * SUB_PAGE_SIZE
    page_items = active_subs[start : start + SUB_PAGE_SIZE]
    text = "\n\n".join(
        [
            (
                f"Subscription: {subscription.id}\n"
                f"Status: {subscription.status}\n"
                f"Used: {subscription.used_bytes}/{subscription.volume_bytes}\n"
                f"Sub Link: {subscription.sub_link or '-'}"
            )
            for subscription in page_items
        ]
    )
    builder = InlineKeyboardBuilder()
    for subscription in page_items:
        builder.button(
            text=f"{AdminButtons.REVOKE_CONFIG} {str(subscription.id)[:8]}",
            callback_data=AdminSubscriptionActionCallback(
                action="revoke",
                subscription_id=subscription.id,
                user_id=user.id,
                page=page,
            ).pack(),
        )
    builder.adjust(1)
    add_pagination_controls(
        builder,
        page=page,
        total_items=len(active_subs),
        page_size=SUB_PAGE_SIZE,
        prev_callback_data=AdminSubscriptionListPageCallback(user_id=user.id, page=page - 1).pack(),
        next_callback_data=AdminSubscriptionListPageCallback(user_id=user.id, page=page + 1).pack(),
    )
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    except TelegramBadRequest:
        await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(AdminSubscriptionActionCallback.filter(F.action == "revoke"))
async def revoke_user_config(
    callback: CallbackQuery,
    callback_data: AdminSubscriptionActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    subscription = await session.scalar(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client)
            .selectinload(XUIClientRecord.inbound)
            .selectinload(XUIInboundRecord.server)
            .selectinload(XUIServerRecord.credentials)
        )
        .where(Subscription.id == callback_data.subscription_id)
    )
    if subscription is None:
        await safe_edit_or_send(callback, AdminMessages.SUBSCRIPTION_NOT_FOUND)
        return

    xui_record = subscription.xui_client
    if xui_record is not None and xui_record.inbound is not None:
        try:
            server = ensure_inbound_server_loaded(xui_record.inbound)
            async with create_xui_client_for_server(server) as xui_client:
                await xui_client.delete_client(
                    inbound_id=xui_record.inbound.xui_inbound_remote_id,
                    client_id=xui_record.xui_client_remote_id or xui_record.client_uuid,
                )
        except Exception as exc:
            logger.error("Failed to delete X-UI client on admin revoke: %s", exc)
        xui_record.is_active = False

    subscription.status = "cancelled"
    subscription.sub_link = None
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="revoke_config",
        entity_type="subscription",
        entity_id=subscription.id,
        payload={"user_id": str(subscription.user_id), "status": "cancelled"},
    )
    await session.flush()
    await _render_user_configs(
        callback=callback,
        session=session,
        user_id=callback_data.user_id,
        page=callback_data.page,
    )
