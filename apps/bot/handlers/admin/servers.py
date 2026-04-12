from __future__ import annotations

import logging
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import AddServerStates
from core.security import encrypt_secret
from core.texts import AdminButtons, AdminMessages, Common
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerCredential, XUIServerRecord
from repositories.audit import AuditLogRepository
from services.xui.client import SanaeiXUIClient, XUIAuthenticationError, XUIClientConfig, XUIRequestError


logger = logging.getLogger(__name__)

router = Router(name="admin-servers")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

SERVER_PAGE_SIZE = 5


class ServerActionCallback(CallbackData, prefix="server"):
    action: str
    server_id: UUID
    page: int = 1


class ServerListPageCallback(CallbackData, prefix="server_list"):
    page: int


@router.message(Command("admin"))
async def admin_main_menu(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.MANAGE_SERVERS, callback_data="admin:servers")
    builder.button(text=AdminButtons.MANAGE_PLANS, callback_data="admin:plans")
    builder.button(text=AdminButtons.MANAGE_USERS, callback_data="admin:users")
    builder.button(text=AdminButtons.BROADCAST, callback_data="admin:broadcast")
    builder.button(text=AdminButtons.MANAGE_RETARGETING, callback_data="admin:retargeting")
    builder.button(text=AdminButtons.STATISTICS, callback_data="admin:stats")
    builder.adjust(1)
    await message.answer(AdminMessages.PANEL_TITLE, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:servers")
async def admin_servers_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.ADD_SERVER, callback_data="admin:servers:add")
    builder.button(text=AdminButtons.LIST_SERVERS, callback_data=ServerListPageCallback(page=1).pack())
    builder.adjust(1)
    await callback.message.answer(AdminMessages.SERVER_MANAGEMENT, reply_markup=builder.as_markup())


@router.callback_query(ServerListPageCallback.filter())
async def list_servers(
    callback: CallbackQuery,
    callback_data: ServerListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    page = max(callback_data.page, 1)
    total_servers = int(await session.scalar(select(func.count()).select_from(XUIServerRecord)) or 0)
    result = await session.execute(
        select(XUIServerRecord)
        .order_by(XUIServerRecord.created_at.asc())
        .offset((page - 1) * SERVER_PAGE_SIZE)
        .limit(SERVER_PAGE_SIZE)
    )
    servers = list(result.scalars().all())

    if not servers:
        text = AdminMessages.NO_SERVERS
        markup = None
    else:
        text = "\n\n".join(
            [
                (
                    f"Server: {server.name}\n"
                    f"Base URL: {server.base_url}\n"
                f"وضعیت: {Common.ACTIVE if server.is_active else Common.INACTIVE}\n"
                f"Health: {server.health_status}"
                )
                for server in servers
            ]
        )
        markup = _build_server_list_keyboard(servers, page=page, total_items=total_servers)

    await _edit_or_send(callback, text=text, reply_markup=markup)


@router.callback_query(F.data == "admin:servers:add")
async def add_server_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddServerStates.waiting_for_name)
    await callback.message.answer(AdminMessages.ENTER_SERVER_NAME)


@router.message(AddServerStates.waiting_for_name)
async def add_server_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddServerStates.waiting_for_base_url)
    await message.answer(AdminMessages.ENTER_SERVER_BASE_URL)


@router.message(AddServerStates.waiting_for_base_url)
async def add_server_base_url(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(base_url=message.text.strip())
    await state.set_state(AddServerStates.waiting_for_username)
    await message.answer(AdminMessages.ENTER_SERVER_USERNAME)


@router.message(AddServerStates.waiting_for_username)
async def add_server_username(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(username=message.text.strip())
    await state.set_state(AddServerStates.waiting_for_password)
    await message.answer(AdminMessages.ENTER_SERVER_PASSWORD)


@router.message(AddServerStates.waiting_for_password)
async def add_server_password(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    form_data = await state.get_data()
    password = message.text.strip()
    base_url = str(form_data["base_url"]).rstrip("/")

    try:
        async with SanaeiXUIClient(
            XUIClientConfig(
                base_url=base_url,
                username=str(form_data["username"]),
                password=SecretStr(password),
                timeout_seconds=15.0,
            )
        ) as xui_client:
            await xui_client.login()
            # Fetch inbounds from the panel right after successful login
            remote_inbounds = await xui_client.get_inbounds()
    except (XUIAuthenticationError, XUIRequestError):
        await message.answer(AdminMessages.SERVER_CONNECTION_FAILED)
        await state.clear()
        return

    server = XUIServerRecord(
        name=str(form_data["name"]),
        base_url=base_url,
        panel_type="sanaei_xui",
        is_active=True,
        health_status="healthy",
    )
    session.add(server)
    await session.flush()

    credential = XUIServerCredential(
        server_id=server.id,
        username=str(form_data["username"]),
        password_encrypted=encrypt_secret(password),
        session_cookie_encrypted=None,
    )
    session.add(credential)
    await session.flush()

    # Auto-sync inbounds from the panel into the database
    synced_count = 0
    for remote_inbound in remote_inbounds:
        # Check if this inbound already exists for this server
        existing = await session.scalar(
            select(XUIInboundRecord).where(
                XUIInboundRecord.server_id == server.id,
                XUIInboundRecord.xui_inbound_remote_id == remote_inbound.id,
            )
        )
        if existing is not None:
            continue

        inbound_record = XUIInboundRecord(
            server_id=server.id,
            xui_inbound_remote_id=remote_inbound.id,
            remark=remote_inbound.remark,
            protocol=remote_inbound.protocol,
            port=remote_inbound.port,
            tag=None,
            is_active=True,
        )
        session.add(inbound_record)
        synced_count += 1

    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="create_server",
        entity_type="server",
        entity_id=server.id,
        payload={"name": server.name, "base_url": server.base_url, "inbounds_synced": synced_count},
    )

    await state.clear()

    inbound_summary = ""
    if synced_count > 0:
        inbound_summary = f"\n\n✅ {synced_count} اینباند از پنل دریافت و ذخیره شد."
    else:
        inbound_summary = "\n\n⚠️ هیچ اینباندی در پنل پیدا نشد."

    await message.answer(
        AdminMessages.SERVER_CREATED.format(name=server.name) + inbound_summary
    )


@router.callback_query(ServerActionCallback.filter(F.action == "sync"))
async def sync_server_inbounds(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    """Re-sync inbounds from an existing server."""
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await callback.message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    if server.credentials is None:
        await callback.message.answer("اطلاعات ورود سرور موجود نیست.")
        return

    from core.security import decrypt_secret
    try:
        async with SanaeiXUIClient(
            XUIClientConfig(
                base_url=server.base_url,
                username=server.credentials.username,
                password=SecretStr(decrypt_secret(server.credentials.password_encrypted)),
                timeout_seconds=15.0,
            )
        ) as xui_client:
            await xui_client.login()
            remote_inbounds = await xui_client.get_inbounds()
    except (XUIAuthenticationError, XUIRequestError) as exc:
        await callback.message.answer(f"خطا در اتصال به پنل: {exc}")
        return

    synced_count = 0
    for remote_inbound in remote_inbounds:
        existing = await session.scalar(
            select(XUIInboundRecord).where(
                XUIInboundRecord.server_id == server.id,
                XUIInboundRecord.xui_inbound_remote_id == remote_inbound.id,
            )
        )
        if existing is not None:
            # Update existing inbound info
            existing.remark = remote_inbound.remark
            existing.protocol = remote_inbound.protocol
            existing.port = remote_inbound.port
            continue

        inbound_record = XUIInboundRecord(
            server_id=server.id,
            xui_inbound_remote_id=remote_inbound.id,
            remark=remote_inbound.remark,
            protocol=remote_inbound.protocol,
            port=remote_inbound.port,
            tag=None,
            is_active=True,
        )
        session.add(inbound_record)
        synced_count += 1

    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="sync_inbounds",
        entity_type="server",
        entity_id=server.id,
        payload={"synced": synced_count, "total_remote": len(remote_inbounds)},
    )

    await callback.message.answer(
        f"✅ سینک اینباندها انجام شد.\n"
        f"اینباندهای جدید ثبت‌شده: {synced_count}\n"
        f"کل اینباندها در پنل: {len(remote_inbounds)}"
    )


@router.callback_query(ServerActionCallback.filter(F.action == "toggle"))
async def toggle_server(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    server = await session.get(XUIServerRecord, callback_data.server_id)
    if server is None:
        await callback.message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    previous_state = server.is_active
    server.is_active = not server.is_active
    server.health_status = "healthy" if server.is_active else "disabled"
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_server",
        entity_type="server",
        entity_id=server.id,
        payload={"from": previous_state, "to": server.is_active},
    )
    await session.flush()
    await list_servers(callback, ServerListPageCallback(page=callback_data.page), session)


@router.callback_query(ServerActionCallback.filter(F.action == "delete"))
async def delete_server(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    server = await session.get(XUIServerRecord, callback_data.server_id)
    if server is None:
        await callback.message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    active_client_count = int(
        await session.scalar(
            select(func.count())
            .select_from(XUIClientRecord)
            .join(XUIInboundRecord, XUIClientRecord.inbound_id == XUIInboundRecord.id)
            .where(
                XUIClientRecord.is_active.is_(True),
                XUIInboundRecord.server_id == server.id,
            )
        ) or 0
    )

    action_payload: dict[str, object]
    if active_client_count > 0:
        server.is_active = False
        server.health_status = "deleted"
        action_payload = {"mode": "soft_delete", "active_clients": active_client_count}
    else:
        action_payload = {"mode": "hard_delete", "active_clients": 0}
        await session.delete(server)

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="delete_server",
        entity_type="server",
        entity_id=callback_data.server_id,
        payload=action_payload,
    )
    await session.flush()
    await list_servers(callback, ServerListPageCallback(page=callback_data.page), session)


def _build_server_list_keyboard(
    servers: list[XUIServerRecord],
    *,
    page: int,
    total_items: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for server in servers:
        builder.button(
            text=f"{server.name} | {'ON' if server.is_active else 'OFF'}",
            callback_data=ServerActionCallback(action="toggle", server_id=server.id, page=page).pack(),
        )
        builder.button(
            text=f"🔄 سینک {server.name}",
            callback_data=ServerActionCallback(action="sync", server_id=server.id, page=page).pack(),
        )
        builder.button(
            text=f"{AdminButtons.DELETE} {server.name}",
            callback_data=ServerActionCallback(action="delete", server_id=server.id, page=page).pack(),
        )
    builder.adjust(1)
    add_pagination_controls(
        builder,
        page=page,
        total_items=total_items,
        page_size=SERVER_PAGE_SIZE,
        prev_callback_data=ServerListPageCallback(page=page - 1).pack(),
        next_callback_data=ServerListPageCallback(page=page + 1).pack(),
    )
    return builder.as_markup()


async def _edit_or_send(
    callback: CallbackQuery,
    *,
    text: str,
    reply_markup,
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=reply_markup)
