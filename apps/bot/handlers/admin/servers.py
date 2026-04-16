from __future__ import annotations

import logging
from collections.abc import Sequence
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
from apps.bot.states.admin import AddServerStates, ServerManageStates
from core.security import decrypt_secret, encrypt_secret
from core.texts import AdminButtons, AdminMessages, Common
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerCredential, XUIServerRecord
from repositories.audit import AuditLogRepository
from services.xui.client import SanaeiXUIClient, XUIAuthenticationError, XUIClientConfig, XUIRequestError
from apps.bot.utils.messaging import safe_edit_or_send


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
@router.message(F.text == "پنل مدیریت ⚙️")
async def admin_main_menu(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.MANAGE_SERVERS, callback_data="admin:servers")
    builder.button(text=AdminButtons.MANAGE_PLANS, callback_data="admin:plans")
    builder.button(text=AdminButtons.MANAGE_USERS, callback_data="admin:users")
    builder.button(text=AdminButtons.BROADCAST, callback_data="admin:broadcast")
    builder.button(text=AdminButtons.MANAGE_TICKETS, callback_data="admin:tickets")
    builder.button(text=AdminButtons.MANAGE_RETARGETING, callback_data="admin:retargeting")
    builder.button(text=AdminButtons.STATISTICS, callback_data="admin:stats")
    builder.button(text=AdminButtons.BOT_SETTINGS, callback_data="admin:bot_settings")
    builder.button(text=AdminButtons.MANAGE_DISCOUNTS, callback_data="admin:discounts")
    builder.adjust(2)
    await message.answer(AdminMessages.PANEL_TITLE, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:main")
async def admin_main_menu_callback(callback: CallbackQuery) -> None:
    """Back button target: re-render admin main menu."""
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.MANAGE_SERVERS, callback_data="admin:servers")
    builder.button(text=AdminButtons.MANAGE_PLANS, callback_data="admin:plans")
    builder.button(text=AdminButtons.MANAGE_USERS, callback_data="admin:users")
    builder.button(text=AdminButtons.BROADCAST, callback_data="admin:broadcast")
    builder.button(text=AdminButtons.MANAGE_TICKETS, callback_data="admin:tickets")
    builder.button(text=AdminButtons.MANAGE_RETARGETING, callback_data="admin:retargeting")
    builder.button(text=AdminButtons.STATISTICS, callback_data="admin:stats")
    builder.button(text=AdminButtons.BOT_SETTINGS, callback_data="admin:bot_settings")
    builder.button(text=AdminButtons.MANAGE_DISCOUNTS, callback_data="admin:discounts")
    builder.adjust(2)
    
    if callback.message:
        try:
            await callback.message.edit_text(AdminMessages.PANEL_TITLE, reply_markup=builder.as_markup())
        except Exception:
            await safe_edit_or_send(callback, AdminMessages.PANEL_TITLE, reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:servers")
async def admin_servers_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.ADD_SERVER, callback_data="admin:servers:add")
    builder.button(text=AdminButtons.LIST_SERVERS, callback_data=ServerListPageCallback(page=1).pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    
    if callback.message:
        try:
            await callback.message.edit_text(AdminMessages.SERVER_MANAGEMENT, reply_markup=builder.as_markup())
        except Exception:
            await safe_edit_or_send(callback, AdminMessages.SERVER_MANAGEMENT, reply_markup=builder.as_markup())


@router.callback_query(ServerListPageCallback.filter())
async def list_servers(
    callback: CallbackQuery,
    callback_data: ServerListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    page = max(callback_data.page, 1)
    total_servers = int(await session.scalar(select(func.count()).select_from(XUIServerRecord).where(XUIServerRecord.health_status != "deleted")) or 0)
    result = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.health_status != "deleted")
        .order_by(XUIServerRecord.created_at.asc())
        .offset((page - 1) * SERVER_PAGE_SIZE)
        .limit(SERVER_PAGE_SIZE)
    )
    servers = list(result.scalars().all())

    if not servers:
        await _edit_or_send(callback, text=AdminMessages.NO_SERVERS, reply_markup=None)
        return

    text = "📋 لیست سرورهای ثبت‌شده:\nلطفاً برای مدیریت، روی سرور مورد نظر کلیک کنید."
    markup = _build_server_list_keyboard(servers, page=page, total_items=total_servers)
    await _edit_or_send(callback, text=text, reply_markup=markup)


@router.callback_query(F.data == "admin:servers:add")
async def add_server_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddServerStates.waiting_for_name)
    await safe_edit_or_send(callback, AdminMessages.ENTER_SERVER_NAME)


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

    # Ensure URL has a scheme
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = "http://" + base_url

    # Try multiple URL variations to find the working one
    urls_to_try = [base_url]
    if base_url.startswith("http://"):
        urls_to_try.append("https://" + base_url[7:])
    elif base_url.startswith("https://"):
        urls_to_try.append("http://" + base_url[8:])

    remote_inbounds = None
    last_error = None
    working_url = base_url
    for url in urls_to_try:
        try:
            logger.info("Trying to connect to X-UI panel at: %s", url)
            remote_inbounds = await _fetch_remote_inbounds(
                base_url=url,
                username=str(form_data["username"]),
                password=password,
            )
            working_url = url
            logger.info("Successfully connected to: %s", url)
            break
        except Exception as exc:
            last_error = exc
            logger.warning("Failed to connect to %s: %s", url, exc)

    if remote_inbounds is None:
        error_detail = str(last_error)[:300] if last_error else "خطای نامشخص"
        await message.answer(
            f"❌ خطا در اتصال به سرور.\n\n"
            f"آدرس‌های امتحان‌شده:\n"
            + "\n".join(f"• {u}" for u in urls_to_try)
            + f"\n\nخطا: {error_detail}"
        )
        await state.clear()
        return

    base_url = working_url

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

    created_inbounds, synced_count, _ = _sync_remote_inbounds(
        server_id=server.id,
        existing_inbounds=[],
        remote_inbounds=remote_inbounds,
    )
    session.add_all(created_inbounds)
    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="create_server",
        entity_type="server",
        entity_id=server.id,
        payload={
            "name": server.name,
            "base_url": server.base_url,
            "inbounds_synced": synced_count,
        },
    )

    await state.clear()
    await message.answer(
        AdminMessages.SERVER_CREATED.format(name=server.name)
        + f"\n\n{synced_count} اینباند از پنل دریافت و ثبت شد."
    )


@router.callback_query(ServerActionCallback.filter(F.action == "sync"))
async def sync_server_inbounds(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials), selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return
    if server.credentials is None:
        await safe_edit_or_send(callback, "اطلاعات ورود سرور موجود نیست.")
        return

    try:
        remote_inbounds = await _fetch_remote_inbounds(
            base_url=server.base_url,
            username=server.credentials.username,
            password=decrypt_secret(server.credentials.password_encrypted),
        )
    except Exception as exc:
        await safe_edit_or_send(callback, f"خطا در اتصال به پنل:\n`{exc}`", parse_mode="MarkdownV2")
        return

    created_inbounds, synced_count, disabled_count = _sync_remote_inbounds(
        server_id=server.id,
        existing_inbounds=server.inbounds,
        remote_inbounds=remote_inbounds,
    )
    session.add_all(created_inbounds)
    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="sync_inbounds",
        entity_type="server",
        entity_id=server.id,
        payload={
            "synced": synced_count,
            "disabled": disabled_count,
            "total_remote": len(remote_inbounds),
        },
    )

    await safe_edit_or_send(callback, 
        "سینک اینباندها انجام شد.\n"
        f"اینباند جدید: {synced_count}\n"
        f"اینباند غیرفعال‌شده: {disabled_count}\n"
        f"کل اینباندهای پنل: {len(remote_inbounds)}"
    )


@router.callback_query(ServerActionCallback.filter(F.action == "toggle"))
async def toggle_server(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return

    previous_state = server.is_active
    server.is_active = not server.is_active
    server.health_status = "healthy" if server.is_active else "disabled"
    if not server.is_active:
        for inbound in server.inbounds:
            inbound.is_active = False

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
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
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

    if active_client_count > 0:
        server.is_active = False
        server.health_status = "deleted"
        for inbound in server.inbounds:
            inbound.is_active = False
        action_payload: dict[str, object] = {"mode": "soft_delete", "active_clients": active_client_count}
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
    if active_client_count > 0:
        await callback.answer(f"سرور با موفقیت بایگانی شد.\n({active_client_count} کاربر فعال روی این سرور وجود دارد و به مرور منقضی می‌شوند)", show_alert=True)
    else:
        await callback.answer("سرور به طور کامل حذف شد.", show_alert=True)
        
    await list_servers(callback, ServerListPageCallback(page=callback_data.page), session)


@router.callback_query(ServerActionCallback.filter(F.action == "manage"))
async def server_manage_menu(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.inbounds))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return

    active_inbounds = sum(1 for inbound in server.inbounds if inbound.is_active)
    
    # Active clients using this server
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

    limit_text = str(server.max_clients) if server.max_clients else "نامحدود"
    status_text = "حذف شده" if server.health_status == "deleted" else (Common.ACTIVE if server.is_active else Common.INACTIVE)

    text = (
        f"🖥 **مدیریت سرور: {server.name}**\n\n"
        f"وضعیت: {status_text}\n"
        f"آدرس: {server.base_url}\n"
        f"دامنه کانفیگ: {server.config_domain or 'تنظیم نشده (پیش‌فرض آدرس پنل)'}\n"
        f"ساب دامین: {server.sub_domain or 'تنظیم نشده (پیش‌فرض آدرس پنل)'}\n\n"
        f"اینباندهای فعال: {active_inbounds}\n"
        f"کاربران فعال روی سرور: {active_client_count} / {limit_text}\n"
    )

    builder = InlineKeyboardBuilder()
    
    if server.health_status != "deleted":
        builder.button(
            text=f"🔄 سینک کردن پنل",
            callback_data=ServerActionCallback(action="sync", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="🛑 تغییر وضعیت (ON/OFF)",
            callback_data=ServerActionCallback(action="toggle", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="✏️ ویرایش آدرس سرور",
            callback_data=ServerActionCallback(action="edit_url", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="🔑 ویرایش اعتبارنامه",
            callback_data=ServerActionCallback(action="edit_creds", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="🌐 تنظیم دامنه‌ها",
            callback_data=ServerActionCallback(action="edit_domain", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text="👥 تنظیم محدودیت کاربر",
            callback_data=ServerActionCallback(action="edit_limit", server_id=server.id, page=callback_data.page).pack(),
        )
        builder.button(
            text=f"{AdminButtons.DELETE} حذف سرور",
            callback_data=ServerActionCallback(action="delete", server_id=server.id, page=callback_data.page).pack(),
        )

    builder.button(
        text="🔙 لیست سرورها",
        callback_data=ServerListPageCallback(page=callback_data.page).pack(),
    )
    
    builder.adjust(2, 2, 2, 1, 1)
    await _edit_or_send(callback, text=text, reply_markup=builder.as_markup())


@router.callback_query(ServerActionCallback.filter(F.action == "edit_domain"))
async def edit_domain_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_config_domain)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="پرش (حذف مقدار)", callback_data="server:domain:skip_config")
    builder.adjust(1)
    
    await safe_edit_or_send(callback, 
        "ابتدا دامنه یا آدرس IP که برای ساخت کانفیگ‌ها (VLESS/VMess) استفاده می‌شود را وارد کنید.\n"
        "(مثلاً proxy.example.com یا آدرس IP تمیز)\n"
        "اگر مقداری ارسال نکنید، از همون آدرس پنل X-UI استفاده می‌شود.",
        reply_markup=builder.as_markup()
    )


@router.message(ServerManageStates.waiting_for_config_domain)
async def edit_domain_config(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(config_domain=message.text.strip())
    await _prompt_sub_domain(message, state)


@router.callback_query(F.data == "server:domain:skip_config")
async def skip_config_domain(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(config_domain=None)
    await _prompt_sub_domain(callback.message, state)


async def _prompt_sub_domain(message: Message, state: FSMContext) -> None:
    await state.set_state(ServerManageStates.waiting_for_sub_domain)
    builder = InlineKeyboardBuilder()
    builder.button(text="پرش (حذف مقدار)", callback_data="server:domain:skip_sub")
    builder.adjust(1)
    await message.answer(
        "حالا ساب‌دامینی که برای لینک‌های اشتراک (Subscription Link) استفاده می‌شود را وارد کنید.\n"
        "(مثلاً sub.example.com)\n"
        "اگر مقداری ارسال نکنید، از همون آدرس پنل X-UI استفاده می‌شود.",
        reply_markup=builder.as_markup()
    )


@router.message(ServerManageStates.waiting_for_sub_domain)
async def edit_domain_sub(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    await _save_domains(message, state, session, message.text.strip())


@router.callback_query(F.data == "server:domain:skip_sub")
async def skip_sub_domain(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _save_domains(callback.message, state, session, None)


async def _save_domains(message: Message, state: FSMContext, session: AsyncSession, sub_domain: str | None) -> None:
    data = await state.get_data()
    await state.clear()
    
    server_id = UUID(data["server_id"])
    config_domain = data.get("config_domain")
    
    server = await session.get(XUIServerRecord, server_id)
    if server:
        server.config_domain = config_domain
        server.sub_domain = sub_domain
        await session.flush()
        
    await message.answer(
        f"✅ دامنه‌های سرور ثبت شد.\n\nدامنه کانفیگ: {config_domain or 'تهی'}\nساب‌دامین: {sub_domain or 'تهی'}"
    )


@router.callback_query(ServerActionCallback.filter(F.action == "edit_limit"))
async def edit_limit_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_max_clients)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="نامحدود (بدون لیمیت)", callback_data="server:limit:unlimited")
    builder.adjust(1)
    
    await safe_edit_or_send(callback, 
        "حداکثر تعداد کلاینت (کاربر فعال) مجاز روی این سرور را به عدد ارسال کنید.\n"
        "مثلاً: 100",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "server:limit:unlimited")
async def limit_unlimited(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _save_limit(callback.message, state, session, None)


@router.message(ServerManageStates.waiting_for_max_clients)
async def edit_limit_value(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return
    try:
        limit = int(message.text.strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("لطفاً یک عدد معتبر و مثبت ارسال کنید.")
        return
    await _save_limit(message, state, session, limit)


async def _save_limit(message: Message, state: FSMContext, session: AsyncSession, limit: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    
    server_id = UUID(data["server_id"])
    server = await session.get(XUIServerRecord, server_id)
    if server:
        server.max_clients = limit
        await session.flush()
        
    await message.answer(
        f"✅ محدودیت کاربر سرور روی {limit if limit is not None else 'نامحدود'} تنظیم شد."
    )


# ─── Edit Server URL ──────────────────────────────────────────────────────────


@router.callback_query(ServerActionCallback.filter(F.action == "edit_url"))
async def edit_url_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    server = await session.get(XUIServerRecord, callback_data.server_id)
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return

    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_new_base_url)
    await safe_edit_or_send(callback, 
        f"آدرس فعلی سرور:\n`{server.base_url}`\n\n"
        "آدرس جدید سرور (به همراه پورت) را وارد کنید:\n"
        "مثلاً: http://1.2.3.4:54321 یا https://panel.example.com:2053",
        parse_mode="Markdown",
    )


@router.message(ServerManageStates.waiting_for_new_base_url)
async def edit_url_receive(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    new_url = message.text.strip().rstrip("/")
    data = await state.get_data()
    server_id = UUID(data["server_id"])

    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == server_id)
    )
    if server is None or server.credentials is None:
        await state.clear()
        await message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    # Test connection with the new URL
    password = decrypt_secret(server.credentials.password_encrypted)
    try:
        await _fetch_remote_inbounds(
            base_url=new_url,
            username=server.credentials.username,
            password=password,
        )
    except Exception as exc:
        await message.answer(
            f"❌ اتصال به آدرس جدید ناموفق بود:\n{exc}\n\nلطفاً آدرس صحیح را وارد کنید."
        )
        return

    old_url = server.base_url
    server.base_url = new_url
    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_server_url",
        entity_type="server",
        entity_id=server.id,
        payload={"old_url": old_url, "new_url": new_url},
    )

    await state.clear()
    await message.answer(
        f"✅ آدرس سرور «{server.name}» عوض شد.\n\n"
        f"قبلی: {old_url}\n"
        f"جدید: {new_url}"
    )


# ─── Edit Server Credentials ──────────────────────────────────────────────────


@router.callback_query(ServerActionCallback.filter(F.action == "edit_creds"))
async def edit_creds_start(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == callback_data.server_id)
    )
    if server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return

    current_username = server.credentials.username if server.credentials else "-"
    await state.update_data(server_id=str(callback_data.server_id), page=callback_data.page)
    await state.set_state(ServerManageStates.waiting_for_new_username)
    await safe_edit_or_send(callback, 
        f"یوزرنیم فعلی: `{current_username}`\n\n"
        "یوزرنیم جدید پنل X-UI را وارد کنید:",
        parse_mode="Markdown",
    )


@router.message(ServerManageStates.waiting_for_new_username)
async def edit_creds_username(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(new_username=message.text.strip())
    await state.set_state(ServerManageStates.waiting_for_new_password)
    await message.answer("رمز عبور جدید پنل X-UI را وارد کنید:")


@router.message(ServerManageStates.waiting_for_new_password)
async def edit_creds_password(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    new_password = message.text.strip()
    data = await state.get_data()
    server_id = UUID(data["server_id"])
    new_username = data["new_username"]

    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == server_id)
    )
    if server is None:
        await state.clear()
        await message.answer(AdminMessages.SERVER_NOT_FOUND)
        return

    # Test connection with new credentials
    try:
        await _fetch_remote_inbounds(
            base_url=server.base_url,
            username=new_username,
            password=new_password,
        )
    except Exception as exc:
        await message.answer(
            f"❌ ورود با اعتبارنامه جدید ناموفق بود:\n{exc}\n\n"
            "لطفاً یوزرنیم و رمز عبور صحیح را وارد کنید."
        )
        await state.set_state(ServerManageStates.waiting_for_new_username)
        await message.answer("یوزرنیم جدید را دوباره وارد کنید:")
        return

    if server.credentials:
        server.credentials.username = new_username
        server.credentials.password_encrypted = encrypt_secret(new_password)
    else:
        cred = XUIServerCredential(
            server_id=server.id,
            username=new_username,
            password_encrypted=encrypt_secret(new_password),
        )
        session.add(cred)

    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_server_credentials",
        entity_type="server",
        entity_id=server.id,
        payload={"new_username": new_username},
    )

    await state.clear()
    await message.answer(
        f"✅ اعتبارنامه سرور «{server.name}» بروزرسانی شد.\n\n"
        f"یوزرنیم: {new_username}\n"
        "رمز عبور: ••••••••"
    )


def _build_server_list_keyboard(
    servers: list[XUIServerRecord],
    *,
    page: int,
    total_items: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for server in servers:
        status_emoji = "✅" if server.is_active else "❌"
        if server.health_status == "deleted":
            status_emoji = "🗑"
        builder.button(
            text=f"🖥 {server.name} {status_emoji}",
            callback_data=ServerActionCallback(action="manage", server_id=server.id, page=page).pack(),
        )
    builder.adjust(1)
    builder.button(text="🔙 بازگشت", callback_data="admin:main")
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
        await safe_edit_or_send(callback, text, reply_markup=reply_markup)


def _sync_remote_inbounds(
    *,
    server_id: UUID,
    existing_inbounds: Sequence[XUIInboundRecord],
    remote_inbounds,
) -> tuple[list[XUIInboundRecord], int, int]:
    remote_by_id = {remote.id: remote for remote in remote_inbounds}
    existing_by_remote_id = {inbound.xui_inbound_remote_id: inbound for inbound in existing_inbounds}
    created: list[XUIInboundRecord] = []
    created_count = 0
    disabled_count = 0

    for remote_id, inbound in existing_by_remote_id.items():
        remote = remote_by_id.get(remote_id)
        if remote is None:
            if inbound.is_active:
                inbound.is_active = False
                disabled_count += 1
            continue

        inbound.remark = remote.remark
        inbound.protocol = remote.protocol
        inbound.port = remote.port
        inbound.is_active = True
        # Store stream settings so config generation can read network type, security, etc.
        inbound.metadata_ = _build_inbound_metadata(remote)

    for remote in remote_inbounds:
        if remote.id in existing_by_remote_id:
            continue
        created.append(
            XUIInboundRecord(
                server_id=server_id,
                xui_inbound_remote_id=remote.id,
                remark=remote.remark,
                protocol=remote.protocol,
                port=remote.port,
                tag=None,
                is_active=True,
                metadata_=_build_inbound_metadata(remote),
            )
        )
        created_count += 1

    return created, created_count, disabled_count


def _build_inbound_metadata(remote) -> dict:
    """Extract stream_settings and other relevant config from the remote inbound."""
    meta: dict = {}
    if remote.stream_settings and isinstance(remote.stream_settings, dict):
        meta["stream_settings"] = remote.stream_settings
    if remote.sniffing and isinstance(remote.sniffing, dict):
        meta["sniffing"] = remote.sniffing
    if remote.settings and isinstance(remote.settings, dict):
        meta["settings"] = remote.settings
    return meta


async def _fetch_remote_inbounds(*, base_url: str, username: str, password: str):
    async with SanaeiXUIClient(
        XUIClientConfig(
            base_url=base_url,
            username=username,
            password=SecretStr(password),
            timeout_seconds=15.0,
        )
    ) as xui_client:
        await xui_client.login()
        return await xui_client.get_inbounds()
