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
from apps.bot.utils.menu_match import MenuText
from models.plan import Plan
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerCredential, XUIServerRecord
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from services.xui.client import SanaeiXUIClient, XUIAuthenticationError, XUIClientConfig, XUIRequestError
from services.panels.adapter import capabilities_for, is_pasarguard
from apps.bot.utils.button_style import styled_button
from apps.bot.utils.messaging import safe_edit_or_send
from apps.bot.utils.panels import admin_panel, status_label


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


def _build_admin_main_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Top management items — neutral "info" role (defaults to blue/primary).
    styled_button(builder, "سرورها", callback_data="admin:servers", role="info")
    styled_button(builder, "پلن‌ها", callback_data="admin:plans", role="info")
    styled_button(builder, "فروش آماده", callback_data="admin:ready_configs", role="info")
    styled_button(builder, "کاربران", callback_data="admin:users", role="info")
    styled_button(builder, "مشتریان", callback_data="admin:customers", role="info")
    # Global config search → assign any config to any member.
    styled_button(builder, "جستجوی کانفیگ", callback_data="admin:config_search", role="info")
    styled_button(builder, "پیام همگانی", callback_data="admin:broadcast", role="info")
    styled_button(builder, "تیکت‌ها", callback_data="admin:tickets", role="info")
    styled_button(builder, "ریتارگتینگ", callback_data="admin:retargeting", role="info")
    # Reporting / money — confirm role (defaults to green/success).
    styled_button(builder, "آمار و گزارش‌ها", callback_data="admin:stats", role="confirm")
    styled_button(builder, "مالی", callback_data="admin:finance", role="confirm")
    styled_button(builder, "هدیه گروهی", callback_data="admin:gifts", role="confirm")
    # Settings / sensitive actions — destructive role (defaults to red/danger).
    styled_button(builder, "تنظیمات ربات", callback_data="admin:bot_settings", role="destructive")
    styled_button(builder, "تخفیف‌ها", callback_data="admin:discounts", role="info")
    styled_button(builder, "بازیابی پرداخت‌ها", callback_data="admin:recovery", role="info")
    styled_button(builder, "دریافت بکاپ", callback_data="admin:backup", role="confirm")
    builder.adjust(2, 2, 2, 2, 2, 2, 2, 2)
    return builder.as_markup()


def _admin_main_text() -> str:
    return admin_panel(
        "پنل مدیریت",
        [
            (
                "بخش‌های اصلی",
                [
                    ("فروش", "سرورها، پلن‌ها، کانفیگ آماده"),
                    ("کاربران", "مشتریان، تیکت‌ها، پیام همگانی"),
                    ("گزارش", "آمار، مالی، بازیابی و بکاپ"),
                ],
            ),
        ],
    )


@router.message(Command("admin"))
# Accept both the new label ("⚙️ پنل مدیریت") and the legacy one
# ("پنل مدیریت ⚙️"), and ignore a leading emoji so it still routes when
# premium-emoji icons strip "⚙️" from the button text.
@router.message(MenuText("⚙️ پنل مدیریت", "پنل مدیریت ⚙️"))
async def admin_main_menu(message: Message) -> None:
    await message.answer(_admin_main_text(), reply_markup=_build_admin_main_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin:main")
async def admin_main_menu_callback(callback: CallbackQuery) -> None:
    """Back button target: re-render admin main menu."""
    await callback.answer()
    text = _admin_main_text()
    markup = _build_admin_main_markup()
    
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await safe_edit_or_send(callback, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "admin:servers")
async def admin_servers_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    text = admin_panel(
        "مدیریت سرورها",
        [
            (
                "عملیات",
                [
                    ("افزودن", "ثبت پنل سنایی جدید"),
                    ("لیست", "مشاهده، سینک و ویرایش سرورها"),
                ],
            ),
        ],
    )
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.ADD_SERVER, callback_data="admin:servers:add")
    builder.button(text=AdminButtons.LIST_SERVERS, callback_data=ServerListPageCallback(page=1).pack())
    builder.button(text="🎯 اینباندهای fallback", callback_data="admin:fallback_inbounds")
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(2, 1, 1)
    
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


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

    text = admin_panel(
        "لیست سرورها",
        [
            (
                "راهنما",
                [
                    ("مدیریت", "برای جزئیات و عملیات روی سرور کلیک کنید"),
                    ("صفحه", page),
                ],
            ),
        ],
    )
    markup = _build_server_list_keyboard(servers, page=page, total_items=total_servers)
    await _edit_or_send(callback, text=text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "admin:servers:add")
async def add_server_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AddServerStates.waiting_for_panel_type)
    builder = InlineKeyboardBuilder()
    builder.button(text="🟦 X-UI (Sanaei)", callback_data="admin:servers:add:type:xui")
    builder.button(text="🟩 PasarGuard", callback_data="admin:servers:add:type:pg")
    builder.button(text=AdminButtons.BACK, callback_data="admin:servers")
    builder.adjust(2, 1)
    await safe_edit_or_send(
        callback,
        "نوعِ پنل را انتخاب کن:\n\n"
        "• <b>X-UI (Sanaei)</b> — همان پنل قبلی\n"
        "• <b>PasarGuard</b> — پنلِ کاربر-محور (مرزبان‌بنیان)",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:servers:add:type:xui")
async def add_server_pick_xui(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(panel_type="sanaei_xui")
    await state.set_state(AddServerStates.waiting_for_name)
    await safe_edit_or_send(callback, AdminMessages.ENTER_SERVER_NAME)


@router.callback_query(F.data == "admin:servers:add:type:pg")
async def add_server_pick_pasarguard(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(panel_type="pasarguard")
    await state.set_state(AddServerStates.waiting_for_name)
    await safe_edit_or_send(
        callback,
        f"{AdminMessages.ENTER_SERVER_NAME}\n\n🟩 پنل: <b>PasarGuard</b>",
        parse_mode="HTML",
    )


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

    # PasarGuard panels use a completely different (user-centric) API, so they
    # get their own connect-and-sync path. The X-UI body below is untouched.
    if str(form_data.get("panel_type") or "sanaei_xui") == "pasarguard":
        await _create_pasarguard_server(
            message=message,
            state=state,
            session=session,
            admin_user=admin_user,
            form_data=form_data,
            urls_to_try=urls_to_try,
            password=password,
        )
        return

    remote_inbounds = None
    last_error = None
    working_url = base_url
    for url in urls_to_try:
        try:
            logger.info("Trying to connect to X-UI panel at: %s", url)
            remote_inbounds, panel_settings = await _fetch_remote_inbounds(
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

    sub_port_str = panel_settings.get("subPort", "")
    try:
        sub_port = int(sub_port_str) if sub_port_str else 2096
    except ValueError:
        sub_port = 2096

    server = XUIServerRecord(
        name=str(form_data["name"]),
        base_url=base_url,
        panel_type="sanaei_xui",
        is_active=True,
        health_status="healthy",
        subscription_port=sub_port,
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


async def _create_pasarguard_server(
    *,
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
    form_data: dict,
    urls_to_try: list[str],
    password: str,
) -> None:
    """Connect to a PasarGuard panel, persist the server + credentials, and sync
    its GROUPS into XUIInboundRecord rows (a group == an 'inbound' for the rest
    of the bot). Mirrors the X-UI add-server tail, but user-centric."""
    groups = None
    last_error = None
    working_url = urls_to_try[0]
    for url in urls_to_try:
        try:
            logger.info("Trying to connect to PasarGuard panel at: %s", url)
            groups = await _fetch_remote_groups(
                base_url=url,
                username=str(form_data["username"]),
                password=password,
            )
            working_url = url
            logger.info("Connected to PasarGuard panel at: %s", url)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("Failed to connect to PasarGuard %s: %s", url, exc)

    if groups is None:
        error_detail = str(last_error)[:300] if last_error else "خطای نامشخص"
        await message.answer(
            "❌ خطا در اتصال به پنلِ PasarGuard.\n\n"
            "آدرس‌های امتحان‌شده:\n"
            + "\n".join(f"• {u}" for u in urls_to_try)
            + f"\n\nخطا: {error_detail}"
        )
        await state.clear()
        return

    server = XUIServerRecord(
        name=str(form_data["name"]),
        base_url=working_url,
        panel_type="pasarguard",
        is_active=True,
        health_status="healthy",
        # PasarGuard serves the subscription on the panel origin itself; the
        # sub_link is taken verbatim from the API, so this port is unused.
        subscription_port=0,
    )
    session.add(server)
    await session.flush()

    session.add(
        XUIServerCredential(
            server_id=server.id,
            username=str(form_data["username"]),
            password_encrypted=encrypt_secret(password),
            session_cookie_encrypted=None,
        )
    )

    created_inbounds, synced_count, _ = _sync_remote_groups(
        server_id=server.id,
        existing_inbounds=[],
        groups=groups,
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
            "panel_type": "pasarguard",
            "groups_synced": synced_count,
        },
    )

    await state.clear()
    await message.answer(
        AdminMessages.SERVER_CREATED.format(name=server.name)
        + f"\n\n🟩 پنل PasarGuard — {synced_count} گروه دریافت و ثبت شد.\n"
        "حالا می‌تونی برای هر گروه یک «پلن» بسازی."
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

    # PasarGuard servers re-sync GROUPS (not X-UI inbounds).
    if is_pasarguard(server):
        try:
            groups = await _fetch_remote_groups(
                base_url=server.base_url,
                username=server.credentials.username,
                password=decrypt_secret(server.credentials.password_encrypted),
            )
        except Exception as exc:  # noqa: BLE001
            await safe_edit_or_send(callback, f"خطا در اتصال به پنل:\n<code>{exc}</code>", parse_mode="HTML")
            return
        created_inbounds, synced_count, disabled_count = _sync_remote_groups(
            server_id=server.id,
            existing_inbounds=server.inbounds,
            groups=groups,
        )
        session.add_all(created_inbounds)
        await session.flush()
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="sync_inbounds",
            entity_type="server",
            entity_id=server.id,
            payload={"synced": synced_count, "disabled": disabled_count, "total_remote": len(groups), "panel_type": "pasarguard"},
        )
        await safe_edit_or_send(
            callback,
            "سینک گروه‌های PasarGuard انجام شد.\n"
            f"گروه جدید: {synced_count}\n"
            f"گروه غیرفعال‌شده: {disabled_count}\n"
            f"کل گروه‌های پنل: {len(groups)}",
        )
        return

    try:
        remote_inbounds, panel_settings = await _fetch_remote_inbounds(
            base_url=server.base_url,
            username=server.credentials.username,
            password=decrypt_secret(server.credentials.password_encrypted),
        )
    except Exception as exc:
        await safe_edit_or_send(callback, f"خطا در اتصال به پنل:\n<code>{exc}</code>", parse_mode="HTML")
        return

    created_inbounds, synced_count, disabled_count = _sync_remote_inbounds(
        server_id=server.id,
        existing_inbounds=server.inbounds,
        remote_inbounds=remote_inbounds,
    )

    sub_port_str = panel_settings.get("subPort", "")
    if sub_port_str:
        try:
            sub_port = int(sub_port_str)
            if server.subscription_port != sub_port:
                server.subscription_port = sub_port
        except ValueError:
            pass
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


@router.callback_query(ServerActionCallback.filter(F.action == "restart"))
async def restart_xray_core_handler(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
    admin_user: User,
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
    if server.credentials is None:
        await safe_edit_or_send(callback, "اطلاعات ورود سرور موجود نیست.")
        return

    if not capabilities_for(server).xray_restart:
        await safe_edit_or_send(callback, "ℹ️ «ریستارت هسته» برای این نوع پنل کاربرد ندارد.")
        return

    from core.config import settings as _app_settings
    try:
        async with SanaeiXUIClient(
            XUIClientConfig(
                base_url=server.base_url,
                username=server.credentials.username,
                password=SecretStr(decrypt_secret(server.credentials.password_encrypted)),
                verify_ssl=_app_settings.xui_verify_ssl,
            )
        ) as client:
            await client.login()
            await client.restart_xray_core()
        
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="restart_xray_core",
            entity_type="server",
            entity_id=server.id,
            payload={},
        )
        await callback.answer("✅ هسته ایکس ری با موفقیت ریستارت شد.", show_alert=True)
    except Exception as exc:
        await safe_edit_or_send(callback, f"❌ خطا در ریستارت هسته:\n<code>{exc}</code>", parse_mode="HTML")


@router.callback_query(ServerActionCallback.filter(F.action == "delete"))
async def delete_server_confirm(
    callback: CallbackQuery,
    callback_data: ServerActionCallback,
    session: AsyncSession,
) -> None:
    """Show a confirmation prompt with active-client count before deleting."""
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

    mode_line = (
        f"⚠️ این سرور <b>{active_client_count}</b> کاربر فعال دارد. "
        "حذف به‌صورت <b>بایگانی</b> انجام می‌شود (سرور غیرفعال می‌ماند)."
        if active_client_count > 0
        else "ℹ️ هیچ کاربر فعالی روی این سرور نیست. حذف کامل خواهد بود."
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❗️ تأیید حذف",
        callback_data=ServerActionCallback(action="del_ok", server_id=server.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="↩️ انصراف",
        callback_data=ServerListPageCallback(page=callback_data.page).pack(),
    )
    builder.adjust(1)
    await safe_edit_or_send(
        callback,
        f"⚠️ <b>تأیید حذف سرور</b>\n━━━━━━━━━━━━━━\n"
        f"نام سرور: <b>{server.name}</b>\n"
        f"{mode_line}\n━━━━━━━━━━━━━━\n"
        "این عمل قابل بازگشت نیست.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(ServerActionCallback.filter(F.action == "del_ok"))
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
    status_text = "حذف شده" if server.health_status == "deleted" else status_label(server.is_active)
    text = admin_panel(
        f"مدیریت سرور: {server.name}",
        [
            (
                "وضعیت",
                [
                    ("سرور", status_text),
                    ("آدرس", server.base_url),
                    ("دامنه کانفیگ", server.config_domain or "پیش‌فرض آدرس پنل"),
                    ("ساب دامین", server.sub_domain or "پیش‌فرض آدرس پنل"),
                ],
            ),
            (
                "ظرفیت",
                [
                    ("اینباند فعال", active_inbounds),
                    ("کاربران فعال", f"{active_client_count} / {limit_text}"),
                ],
            ),
        ],
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
            text="⚡ ריستارت هسته",
            callback_data=ServerActionCallback(action="restart", server_id=server.id, page=callback_data.page).pack(),
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
    await _edit_or_send(callback, text=text, reply_markup=builder.as_markup(), parse_mode="HTML")


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
        f"آدرس فعلی سرور:\n<code>{server.base_url}</code>\n\n"
        "آدرس جدید سرور (به همراه پورت) را وارد کنید:\n"
        "مثلاً: http://1.2.3.4:54321 یا https://panel.example.com:2053",
        parse_mode="HTML",
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

    # Test connection with the new URL (panel-aware).
    password = decrypt_secret(server.credentials.password_encrypted)
    try:
        if is_pasarguard(server):
            await _fetch_remote_groups(
                base_url=new_url,
                username=server.credentials.username,
                password=password,
            )
        else:
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

    # Test connection with new credentials (panel-aware).
    try:
        if is_pasarguard(server):
            await _fetch_remote_groups(
                base_url=server.base_url,
                username=new_username,
                password=new_password,
            )
        else:
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
    parse_mode: str | None = None,
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        await safe_edit_or_send(callback, text, reply_markup=reply_markup, parse_mode=parse_mode)


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
    from core.config import settings as _app_settings
    async with SanaeiXUIClient(
        XUIClientConfig(
            base_url=base_url,
            username=username,
            password=SecretStr(password),
            timeout_seconds=15.0,
            verify_ssl=_app_settings.xui_verify_ssl,
        )
    ) as xui_client:
        await xui_client.login()
        inbounds = await xui_client.get_inbounds()
        try:
            settings = await xui_client.get_panel_settings()
        except Exception:
            settings = {}
        return inbounds, settings


async def _fetch_remote_groups(*, base_url: str, username: str, password: str):
    """Log in to a PasarGuard panel and return its groups (inbound bundles)."""
    from core.config import settings as _app_settings
    from services.pasarguard.client import PasarGuardClient, PasarGuardClientConfig

    async with PasarGuardClient(
        PasarGuardClientConfig(
            base_url=base_url,
            username=username,
            password=SecretStr(password),
            timeout_seconds=15.0,
            verify_ssl=_app_settings.pasarguard_verify_ssl,
        )
    ) as client:
        await client.login()
        return await client.get_groups()


def _sync_remote_groups(
    *,
    server_id: UUID,
    existing_inbounds: Sequence[XUIInboundRecord],
    groups,
) -> tuple[list[XUIInboundRecord], int, int]:
    """Map PasarGuard groups onto XUIInboundRecord rows (one row per group), so
    the rest of the bot (plan picker, provisioning) treats a group like an
    inbound. Same shape/return as _sync_remote_inbounds."""
    remote_by_id = {g.id: g for g in groups}
    existing_by_remote_id = {ib.xui_inbound_remote_id: ib for ib in existing_inbounds}
    created: list[XUIInboundRecord] = []
    created_count = 0
    disabled_count = 0

    for remote_id, inbound in existing_by_remote_id.items():
        g = remote_by_id.get(remote_id)
        if g is None:
            if inbound.is_active:
                inbound.is_active = False
                disabled_count += 1
            continue
        inbound.remark = g.name
        inbound.tag = g.name
        inbound.protocol = "pasarguard"
        inbound.port = None
        inbound.is_active = not g.is_disabled
        inbound.metadata_ = {"pasarguard_group": True, "inbound_tags": list(g.inbound_tags or [])}

    for g in groups:
        if g.id in existing_by_remote_id:
            continue
        created.append(
            XUIInboundRecord(
                server_id=server_id,
                xui_inbound_remote_id=g.id,
                remark=g.name,
                protocol="pasarguard",
                port=None,
                tag=g.name,
                is_active=not g.is_disabled,
                metadata_={"pasarguard_group": True, "inbound_tags": list(g.inbound_tags or [])},
            )
        )
        created_count += 1

    return created, created_count, disabled_count


# ─────────────────────────────────────────────────────────────────────────────
#  Migration target inbounds (admin UI)
# ─────────────────────────────────────────────────────────────────────────────
#
# Admin toggles which inbounds appear in the user-facing "🛠 تغییر سرور"
# picker. State lives in AppSettings under service.migration_targets — see
# repositories/settings.py::get_migration_target_inbound_ids.

class FallbackToggleCallback(CallbackData, prefix="fbtog"):
    inbound_id: UUID


class PivotPlansCallback(CallbackData, prefix="fbpiv"):
    """Confirm/execute the "point every active plan at this inbound" action.

    Two-stage: action='ask' shows a confirmation prompt with affected plan
    count. action='do' actually performs the bulk UPDATE.
    """
    action: str  # 'ask' | 'do'
    inbound_id: UUID


def _fallback_inbound_label(inbound: XUIInboundRecord, is_selected: bool) -> str:
    prefix = "✅ " if is_selected else "⬜️ "
    parts: list[str] = []
    if inbound.server is not None and inbound.server.name:
        parts.append(inbound.server.name)
    if inbound.remark:
        parts.append(inbound.remark)
    elif inbound.tag:
        parts.append(inbound.tag)
    elif inbound.xui_inbound_remote_id is not None:
        parts.append(f"#{inbound.xui_inbound_remote_id}")
    proto_port: list[str] = []
    if inbound.protocol:
        proto_port.append(str(inbound.protocol))
    if inbound.port:
        proto_port.append(str(inbound.port))
    body = " · ".join(parts) if parts else "اینباند"
    if proto_port:
        body = f"{body} ({':'.join(proto_port)})"
    return (prefix + body)[:60]


@router.callback_query(F.data == "admin:fallback_inbounds")
async def admin_fallback_inbounds(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    """List every active inbound with two controls per row:
      • ✅/⬜️ toggle — user-side "switch server" picker eligibility
      • 🔄 pivot — point every active plan at this inbound (= every
        new purchase from now on goes here, existing configs stay).
    """
    await callback.answer()

    inbounds_result = await session.execute(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .join(XUIServerRecord, XUIInboundRecord.server_id == XUIServerRecord.id)
        .where(
            XUIInboundRecord.is_active.is_(True),
            XUIServerRecord.is_active.is_(True),
        )
        .order_by(XUIServerRecord.name.asc(), XUIInboundRecord.xui_inbound_remote_id.asc())
    )
    inbounds = list(inbounds_result.scalars().unique().all())

    settings_repo = AppSettingsRepository(session)
    selected_raw = await settings_repo.get_migration_target_inbound_ids()
    selected: set[str] = set(selected_raw)

    # Count how many active plans currently point at each inbound, so the
    # admin can tell which one is the "production" inbound at a glance.
    from sqlalchemy import func as _func
    plan_counts_raw = await session.execute(
        select(Plan.inbound_id, _func.count())
        .where(Plan.is_active.is_(True), Plan.inbound_id.isnot(None))
        .group_by(Plan.inbound_id)
    )
    plan_counts: dict[str, int] = {str(pid): int(c) for pid, c in plan_counts_raw.all() if pid is not None}

    builder = InlineKeyboardBuilder()

    if not inbounds:
        text = (
            "🎯 <b>اینباندهای fallback</b>\n"
            "━━━━━━━━━━━━━━\n"
            "❌ هیچ اینباند فعالی پیدا نشد.\n\n"
            "ابتدا یک سرور و اینباند از منوی «🖥 مدیریت سرورها» اضافه کنید."
        )
        builder.button(text=AdminButtons.BACK, callback_data="admin:servers")
        builder.adjust(1)
        await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")
        return

    # Two rows per inbound: toggle + pivot
    layout: list[int] = []
    for inbound in inbounds:
        sid = str(inbound.id)
        builder.button(
            text=_fallback_inbound_label(inbound, sid in selected),
            callback_data=FallbackToggleCallback(inbound_id=inbound.id).pack(),
        )
        pc = plan_counts.get(sid, 0)
        pivot_label = (
            f"🔄 همه‌ی پلن‌ها به این اینباند"
            if pc == 0
            else f"🔄 پلن‌های فعلی این اینباند: {pc} — انتقال همه پلن‌ها"
        )
        builder.button(
            text=pivot_label,
            callback_data=PivotPlansCallback(action="ask", inbound_id=inbound.id).pack(),
        )
        layout.extend([1, 1])

    builder.button(text=AdminButtons.BACK, callback_data="admin:servers")
    layout.append(1)
    builder.adjust(*layout)

    count = len(selected.intersection({str(i.id) for i in inbounds}))
    if count == 0:
        scope_line = (
            "⚠️ هیچ اینباندی به‌عنوان fallback انتخاب نشده — کاربران الان "
            "<b>همه‌ی</b> اینباندها را در لیست انتقال می‌بینند."
        )
    else:
        scope_line = (
            f"✅ <b>{count}</b> اینباند به‌عنوان fallback انتخاب شده — کاربران "
            "فقط همین‌ها را در لیست انتقال می‌بینند."
        )

    text = (
        "🎯 <b>اینباندهای fallback</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>دو کنترل برای هر اینباند:</b>\n"
        "▫️ <b>✅/⬜️ ردیف اول</b> — فعال‌بودن در لیست «🛠 تغییر سرور» کاربرها.\n"
        "▫️ <b>🔄 ردیف دوم</b> — همه پلن‌های فعال را به این اینباند منتقل می‌کند "
        "(یعنی <b>خریدهای جدید از این به بعد</b> روی این اینباند می‌روند). "
        "کانفیگ‌های موجود دست‌نخورده می‌مانند.\n"
        f"\n{scope_line}"
    )

    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(FallbackToggleCallback.filter())
async def admin_fallback_toggle(
    callback: CallbackQuery,
    callback_data: FallbackToggleCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    """Flip a single inbound's membership in the migration target list."""
    settings_repo = AppSettingsRepository(session)
    sid = str(callback_data.inbound_id)
    # Validate that the inbound still exists + is active before toggling
    inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.id == callback_data.inbound_id)
    )
    if inbound is None:
        await callback.answer("این اینباند یافت نشد.", show_alert=True)
        return

    now_enabled = await settings_repo.toggle_migration_target_inbound(sid)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="toggle_fallback_inbound",
            entity_type="inbound",
            entity_id=callback_data.inbound_id,
            payload={"enabled": now_enabled},
        )
    except Exception as exc:
        logger.warning("Audit log failed for fallback toggle: %s", exc)

    await callback.answer(
        "✅ به‌عنوان fallback تنظیم شد." if now_enabled else "⬜️ از لیست fallback خارج شد.",
    )
    # Re-render the list so the user sees the new ✅/⬜️ state.
    await admin_fallback_inbounds(callback, session)


# ─── Pivot all active plans to a single inbound ───────────────────────────
#
# Phase A migration helper. Use case: admin builds a fresh inbound on the
# new server and wants every new purchase from now on to go to it,
# without touching the configs that already exist on the old inbound.
# Implementation is a single bulk UPDATE on plans.inbound_id; existing
# Subscription rows + their XUIClientRecord rows stay untouched.

@router.callback_query(PivotPlansCallback.filter(F.action == "ask"))
async def admin_pivot_plans_ask(
    callback: CallbackQuery,
    callback_data: PivotPlansCallback,
    session: AsyncSession,
) -> None:
    """Stage 1: show what's about to change and ask for explicit confirmation."""
    await callback.answer()

    inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.id == callback_data.inbound_id)
    )
    if inbound is None or inbound.server is None:
        await safe_edit_or_send(callback, AdminMessages.SERVER_NOT_FOUND)
        return

    # How many active plans currently point somewhere ELSE and would move?
    from sqlalchemy import func as _func
    total_active_plans = int(
        await session.scalar(
            select(_func.count()).select_from(Plan).where(Plan.is_active.is_(True))
        ) or 0
    )
    moving_count = int(
        await session.scalar(
            select(_func.count()).select_from(Plan).where(
                Plan.is_active.is_(True),
                (Plan.inbound_id != callback_data.inbound_id) | (Plan.inbound_id.is_(None)),
            )
        ) or 0
    )
    already_count = total_active_plans - moving_count

    target_label = _fallback_inbound_label(inbound, False).lstrip("✅ ⬜️").strip()

    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ تأیید — همه‌ی پلن‌ها روی این اینباند",
        callback_data=PivotPlansCallback(action="do", inbound_id=callback_data.inbound_id).pack(),
    )
    builder.button(text="❌ انصراف", callback_data="admin:fallback_inbounds")
    builder.adjust(1)

    text = (
        "🔄 <b>تأیید انتقال همه‌ی پلن‌ها</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🎯 اینباند مقصد: <code>{target_label}</code>\n"
        f"📦 پلن‌های فعال کل: <b>{total_active_plans}</b>\n"
        f"➡️ منتقل می‌شوند: <b>{moving_count}</b>\n"
        f"✓ از قبل روی این اینباند: <b>{already_count}</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>اثرها:</b>\n"
        "• <b>هر خرید جدید</b> از این به بعد روی این اینباند انجام می‌شود.\n"
        "• <b>کانفیگ‌های موجود</b> دست‌نخورده می‌مانند (روی اینباند فعلی خود).\n"
        "• کاربران می‌توانند خودشان از منوی «🛠 تغییر سرور» کانفیگ‌های قبلی را منتقل کنند.\n\n"
        "آیا تأیید می‌کنید؟"
    )
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(PivotPlansCallback.filter(F.action == "do"))
async def admin_pivot_plans_do(
    callback: CallbackQuery,
    callback_data: PivotPlansCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    """Stage 2: actually perform the bulk update."""
    await callback.answer("⏳ در حال انتقال…")

    # Distributed lock so a double-click (or two admins clicking on the
    # same inbound at the same moment) can't run the bulk update twice.
    # The UPDATE is idempotent (second run hits no-op branch) but the
    # extra round-trip + audit-log churn is noise we don't want.
    from core.redis import distributed_lock
    lock_key = f"lock:pivot_plans:{callback_data.inbound_id}"
    async with distributed_lock(lock_key, ttl_seconds=30) as acquired:
        if not acquired:
            await callback.answer(
                "⏳ یک عملیات انتقال در حال انجام است. چند لحظه صبر کنید.",
                show_alert=True,
            )
            return

        inbound = await session.scalar(
            select(XUIInboundRecord)
            .options(selectinload(XUIInboundRecord.server))
            .where(XUIInboundRecord.id == callback_data.inbound_id)
        )
        if inbound is None or not inbound.is_active:
            await safe_edit_or_send(callback, "❌ اینباند مقصد فعال نیست.")
            return
        if inbound.server is None or not inbound.server.is_active:
            await safe_edit_or_send(callback, "❌ سرور اینباند مقصد فعال نیست.")
            return

        from sqlalchemy import update as _update

        # Snapshot what we're about to change for the audit log + UI message.
        affected_rows = await session.execute(
            select(Plan.id, Plan.name, Plan.inbound_id).where(
                Plan.is_active.is_(True),
                (Plan.inbound_id != callback_data.inbound_id) | (Plan.inbound_id.is_(None)),
            )
        )
        affected = list(affected_rows.all())
        affected_ids = [row.id for row in affected]

        if not affected_ids:
            await safe_edit_or_send(
                callback,
                "ℹ️ همه‌ی پلن‌های فعال از قبل روی این اینباند هستند. تغییری اعمال نشد.",
            )
            return

        # Bulk update — single SQL statement, transactional.
        await session.execute(
            _update(Plan)
            .where(Plan.id.in_(affected_ids))
            .values(inbound_id=callback_data.inbound_id)
        )
        await session.flush()

        try:
            await AuditLogRepository(session).log_action(
                actor_user_id=admin_user.id,
                action="pivot_plans_to_inbound",
                entity_type="inbound",
                entity_id=callback_data.inbound_id,
                payload={
                    "moved_plan_count": len(affected_ids),
                    "moved_plan_ids": [str(pid) for pid in affected_ids],
                    # truncate to keep payload sane
                    "plan_names_preview": [row.name for row in affected[:20]],
                },
            )
        except Exception as exc:
            logger.warning("Audit log failed for pivot_plans_to_inbound: %s", exc)

        target_label = _fallback_inbound_label(inbound, False).lstrip("✅ ⬜️").strip()

        builder = InlineKeyboardBuilder()
        builder.button(text="↩️ بازگشت به لیست اینباندها", callback_data="admin:fallback_inbounds")
        builder.adjust(1)

        text = (
            "✅ <b>انتقال انجام شد!</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"🎯 اینباند مقصد: <code>{target_label}</code>\n"
            f"📦 پلن‌های منتقل‌شده: <b>{len(affected_ids)}</b>\n"
            "━━━━━━━━━━━━━━\n"
            "از این پس، هر خرید جدید روی این اینباند ساخته می‌شود.\n"
            "کانفیگ‌های موجود تغییری نکرده‌اند."
        )
        await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")
