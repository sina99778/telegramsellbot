from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
import unicodedata
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import CreatePlanStates
from core.formatting import format_volume_bytes
from core.texts import AdminButtons, AdminMessages, Buttons, Common
from models.plan import Plan
from models.user import User
from models.xui import XUIInboundRecord
from repositories.audit import AuditLogRepository
from apps.bot.utils.messaging import safe_edit_or_send


router = Router(name="admin-plans")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

logger = logging.getLogger(__name__)

PLAN_PAGE_SIZE = 5


class PlanActionCallback(CallbackData, prefix="plan_admin"):
    action: str
    plan_id: UUID
    page: int = 1


class PlanListPageCallback(CallbackData, prefix="plan_list"):
    page: int


class InboundSelectCallback(CallbackData, prefix="inbound_sel"):
    inbound_id: UUID


MENU_INTERRUPT_TEXTS = {
    Buttons.BUY_CONFIG,
    Buttons.PROFILE_WALLET,
    Buttons.SUPPORT,
    Buttons.MY_CONFIGS,
}


DECIMAL_SEPARATORS = {".", ",", "\u066b", "\u066c", "\u060c"}


@router.message(Command("cancel"), CreatePlanStates.waiting_for_inbound_selection)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_name)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_duration_days)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_volume_gb)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_price)
async def cancel_plan_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(AdminMessages.PLAN_CREATION_CANCELLED)


@router.message(CreatePlanStates.waiting_for_inbound_selection, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_name, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_duration_days, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_volume_gb, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_price, F.text.in_(MENU_INTERRUPT_TEXTS))
async def interrupt_plan_creation_with_main_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(AdminMessages.PLAN_CREATION_INTERRUPTED)


@router.callback_query(F.data == "admin:plans")
async def admin_plans_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.CREATE_PLAN, callback_data="admin:plans:create")
    builder.button(text=AdminButtons.LIST_PLANS, callback_data=PlanListPageCallback(page=1).pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(callback, AdminMessages.PLAN_MANAGEMENT, reply_markup=builder.as_markup())


@router.callback_query(PlanListPageCallback.filter())
async def list_plans(
    callback: CallbackQuery,
    callback_data: PlanListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    page = max(callback_data.page, 1)
    total_plans = int(await session.scalar(select(func.count()).select_from(Plan)) or 0)
    result = await session.execute(
        select(Plan)
        .options(selectinload(Plan.inbound))
        .order_by(Plan.created_at.asc())
        .offset((page - 1) * PLAN_PAGE_SIZE)
        .limit(PLAN_PAGE_SIZE)
    )
    plans = list(result.scalars().all())

    if not plans:
        text = AdminMessages.NO_PLANS
        markup = None
    else:
        text = "\n\n".join(
            (
                f"پلن: {plan.name}\n"
                f"پروتکل: {plan.protocol}\n"
                f"اینباند: {plan.inbound.remark if plan.inbound else 'نامشخص'} "
                f"(ID: {plan.inbound.xui_inbound_remote_id if plan.inbound else '-'})\n"
                f"مدت: {plan.duration_days} روز\n"
                f"حجم: {format_volume_bytes(plan.volume_bytes)}\n"
                f"قیمت: {plan.price} {plan.currency}\n"
                f"وضعیت: {Common.ACTIVE if plan.is_active else Common.INACTIVE}"
            )
            for plan in plans
        )
        markup = _build_plan_list_keyboard(plans, page=page, total_items=total_plans)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await safe_edit_or_send(callback, text, reply_markup=markup)


@router.callback_query(F.data == "admin:plans:create")
async def create_plan_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()

    result = await session.execute(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .join(XUIInboundRecord.server)
        .where(
            XUIInboundRecord.is_active.is_(True),
            XUIInboundRecord.server.has(is_active=True),
        )
        .order_by(XUIInboundRecord.created_at.asc())
    )
    inbounds = list(result.scalars().all())

    if not inbounds:
        await safe_edit_or_send(callback, 
            "هیچ اینباند فعالی موجود نیست.\n"
            "ابتدا یک سرور اضافه یا سینک کنید تا اینباندها از پنل دریافت شوند."
        )
        return

    builder = InlineKeyboardBuilder()
    for inbound in inbounds:
        server_name = inbound.server.name if inbound.server else "?"
        label = (
            f"{inbound.remark or 'بدون نام'} | "
            f"{inbound.protocol or '?'} | "
            f"Port: {inbound.port or '?'} | "
            f"سرور: {server_name}"
        )
        builder.button(
            text=label,
            callback_data=InboundSelectCallback(inbound_id=inbound.id).pack(),
        )
    builder.adjust(1)

    await state.set_state(CreatePlanStates.waiting_for_inbound_selection)
    await safe_edit_or_send(callback, 
        "اینباند مورد نظر را برای این پلن انتخاب کنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(
    CreatePlanStates.waiting_for_inbound_selection,
    InboundSelectCallback.filter(),
)
async def create_plan_inbound_selected(
    callback: CallbackQuery,
    callback_data: InboundSelectCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()

    inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(
            XUIInboundRecord.id == callback_data.inbound_id,
            XUIInboundRecord.is_active.is_(True),
            XUIInboundRecord.server.has(is_active=True),
        )
    )
    if inbound is None:
        await safe_edit_or_send(callback, "اینباند پیدا نشد.")
        await state.clear()
        return

    await state.update_data(
        inbound_id=str(inbound.id),
        protocol=inbound.protocol or "unknown",
        inbound_remote_id=inbound.xui_inbound_remote_id,
    )
    await state.set_state(CreatePlanStates.waiting_for_name)
    await safe_edit_or_send(callback, AdminMessages.ENTER_PLAN_NAME)


@router.message(CreatePlanStates.waiting_for_name)
async def create_plan_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(CreatePlanStates.waiting_for_duration_days)
    await message.answer(AdminMessages.ENTER_DURATION)


@router.message(CreatePlanStates.waiting_for_duration_days)
async def create_plan_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        duration_days = int(_normalize_integer_input(message.text))
    except ValueError:
        await message.answer(AdminMessages.INVALID_INTEGER)
        return
    if duration_days <= 0:
        await message.answer(AdminMessages.DURATION_GT_ZERO)
        return
    await state.update_data(duration_days=duration_days)
    await state.set_state(CreatePlanStates.waiting_for_volume_gb)
    await message.answer(AdminMessages.ENTER_VOLUME)


@router.message(CreatePlanStates.waiting_for_volume_gb)
async def create_plan_volume(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        volume_gb = int(_normalize_integer_input(message.text))
    except ValueError:
        await message.answer(AdminMessages.INVALID_INTEGER)
        return
    if volume_gb <= 0:
        await message.answer(AdminMessages.VOLUME_GT_ZERO)
        return
    await state.update_data(volume_gb=volume_gb)
    await state.set_state(CreatePlanStates.waiting_for_price)
    await message.answer(AdminMessages.ENTER_PRICE)


@router.message(CreatePlanStates.waiting_for_price)
async def create_plan_price(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return

    try:
        price = Decimal(_normalize_decimal_input(message.text))
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    if price <= Decimal("0"):
        await message.answer(AdminMessages.PRICE_GT_ZERO)
        return

    form_data = await state.get_data()
    volume_bytes = int(form_data["volume_gb"]) * 1024 * 1024 * 1024
    protocol = str(form_data["protocol"])
    inbound_id = UUID(str(form_data["inbound_id"]))
    # Include a UUID fragment so code is always globally unique,
    # even if the admin creates two plans with identical parameters.
    code = (
        f"{protocol}_{inbound_id.hex[:8]}_{int(form_data['duration_days'])}d_"
        f"{int(form_data['volume_gb'])}gb_{price.normalize()}_{uuid4().hex[:6]}"
    )

    plan = Plan(
        code=code,
        name=str(form_data["name"]),
        protocol=protocol,
        inbound_id=inbound_id,
        duration_days=int(form_data["duration_days"]),
        volume_bytes=volume_bytes,
        price=price,
        renewal_price=price,
        currency="USD",
        is_active=True,
    )

    # Always clear state first so admin is NEVER stuck regardless of what happens next
    await state.clear()

    try:
        async with session.begin_nested():
            session.add(plan)
            await session.flush()
    except IntegrityError:
        await message.answer(AdminMessages.PLAN_CODE_EXISTS)
        return
    except SQLAlchemyError as exc:
        logger.error("Plan creation DB error: %s", exc, exc_info=True)
        await message.answer(
            "❌ خطای دیتابیس در ساخت پلن.\n"
            "احتمالاً جدول plans نیاز به آپدیت schema دارد.\n"
            "از منوی installer → Database Tools → Bootstrap Schema را اجرا کنید."
        )
        return

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="create_plan",
            entity_type="plan",
            entity_id=plan.id,
            payload={
                "code": plan.code,
                "price": str(plan.price),
                "inbound_id": str(inbound_id),
                "protocol": protocol,
            },
        )
    except Exception as exc:
        logger.warning("Audit log failed after plan creation: %s", exc)

    await message.answer(AdminMessages.PLAN_CREATED.format(name=plan.name))


@router.callback_query(PlanActionCallback.filter(F.action == "toggle"))
async def toggle_plan(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    previous_state = plan.is_active
    plan.is_active = not plan.is_active
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="toggle_plan",
        entity_type="plan",
        entity_id=plan.id,
        payload={"from": previous_state, "to": plan.is_active},
    )
    await session.flush()
    await list_plans(callback, PlanListPageCallback(page=callback_data.page), session)


@router.callback_query(PlanActionCallback.filter(F.action == "delete"))
async def delete_plan(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    plan_name = plan.name

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="delete_plan",
            entity_type="plan",
            entity_id=plan.id,
            payload={"name": plan_name},
        )
        await session.delete(plan)
        await session.flush()
        await safe_edit_or_send(callback, f"✅ پلن «{plan_name}» با موفقیت حذف شد.")
    except IntegrityError:
        await session.rollback()
        # FK constraint: orders reference this plan, so we can't delete it.
        # Deactivate instead.
        plan_again = await session.get(Plan, callback_data.plan_id)
        if plan_again:
            plan_again.is_active = False
            await session.flush()
        await safe_edit_or_send(callback, 
            f"⚠️ پلن «{plan_name}» اردرهای مرتبط دارد و قابل حذف نیست.\n"
            "به جای حذف، غیرفعال شد."
        )
    except Exception as exc:
        logger.error("Failed to delete plan %s: %s", callback_data.plan_id, exc, exc_info=True)
        await safe_edit_or_send(callback, f"❌ خطا در حذف پلن: {exc}")
        return

    await list_plans(callback, PlanListPageCallback(page=callback_data.page), session)


class ChangeInboundCallback(CallbackData, prefix="plan_inb"):
    plan_id: UUID
    inbound_id: UUID
    page: int = 1


@router.callback_query(PlanActionCallback.filter(F.action == "change_inbound"))
async def change_inbound_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    plan = await session.scalar(
        select(Plan)
        .options(selectinload(Plan.inbound).selectinload(XUIInboundRecord.server))
        .where(Plan.id == callback_data.plan_id)
    )
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    # Current inbound info
    if plan.inbound and plan.inbound.server:
        current_info = f"{plan.inbound.server.name} — {plan.inbound.remark} ({plan.inbound.protocol})"
    elif plan.inbound:
        current_info = f"{plan.inbound.remark} ({plan.inbound.protocol})"
    else:
        current_info = "تنظیم نشده ❌"

    # Get all active inbounds
    from models.xui import XUIServerRecord
    result = await session.execute(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.is_active.is_(True))
        .order_by(XUIInboundRecord.created_at.asc())
    )
    inbounds = list(result.scalars().all())

    if not inbounds:
        await safe_edit_or_send(callback, "❌ هیچ اینباند فعالی وجود ندارد. اول سرور اضافه کنید.")
        return

    builder = InlineKeyboardBuilder()
    for inb in inbounds:
        server_name = inb.server.name if inb.server else "نامشخص"
        is_current = "✅ " if plan.inbound_id == inb.id else ""
        builder.button(
            text=f"{is_current}{server_name} — {inb.remark} ({inb.protocol})",
            callback_data=ChangeInboundCallback(
                plan_id=plan.id,
                inbound_id=inb.id,
                page=callback_data.page,
            ).pack(),
        )
    builder.button(
        text="🔙 بازگشت",
        callback_data=PlanListPageCallback(page=callback_data.page).pack(),
    )
    builder.adjust(1)

    await safe_edit_or_send(callback, 
        f"🔗 تغییر سرور برای پلن «{plan.name}»\n\n"
        f"سرور فعلی: {current_info}\n\n"
        "اینباند جدید را انتخاب کنید:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(ChangeInboundCallback.filter())
async def change_inbound_confirm(
    callback: CallbackQuery,
    callback_data: ChangeInboundCallback,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    new_inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.id == callback_data.inbound_id)
    )
    if new_inbound is None:
        await safe_edit_or_send(callback, "❌ اینباند پیدا نشد.")
        return

    old_inbound_id = plan.inbound_id
    plan.inbound_id = new_inbound.id
    await session.flush()

    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="change_plan_inbound",
        entity_type="plan",
        entity_id=plan.id,
        payload={
            "old_inbound_id": str(old_inbound_id) if old_inbound_id else None,
            "new_inbound_id": str(new_inbound.id),
            "new_server": new_inbound.server.name if new_inbound.server else None,
        },
    )

    server_name = new_inbound.server.name if new_inbound.server else "نامشخص"
    await safe_edit_or_send(callback, 
        f"✅ سرور پلن «{plan.name}» تغییر کرد.\n\n"
        f"🖥 سرور: {server_name}\n"
        f"📡 اینباند: {new_inbound.remark} ({new_inbound.protocol})"
    )

    await list_plans(callback, PlanListPageCallback(page=callback_data.page), session)


def _build_plan_list_keyboard(
    plans: list[Plan],
    *,
    page: int,
    total_items: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        builder.button(
            text=f"{plan.name} | {'ON' if plan.is_active else 'OFF'}",
            callback_data=PlanActionCallback(action="toggle", plan_id=plan.id, page=page).pack(),
        )
        builder.button(
            text=f"🔗 سرور",
            callback_data=PlanActionCallback(action="change_inbound", plan_id=plan.id, page=page).pack(),
        )
        builder.button(
            text=f"✖ حذف",
            callback_data=PlanActionCallback(action="delete", plan_id=plan.id, page=page).pack(),
        )
    builder.adjust(3)
    add_pagination_controls(
        builder,
        page=page,
        total_items=total_items,
        page_size=PLAN_PAGE_SIZE,
        prev_callback_data=PlanListPageCallback(page=page - 1).pack(),
        next_callback_data=PlanListPageCallback(page=page + 1).pack(),
    )
    return builder.as_markup()


def _normalize_decimal_input(raw_value: str) -> str:
    normalized_characters: list[str] = []
    seen_decimal_separator = False

    for character in raw_value.strip():
        if character in {"\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u2066", "\u2067", "\u2069"}:
            continue
        if character.isspace():
            continue
        if character in "+-":
            normalized_characters.append(character)
            continue
        if character in DECIMAL_SEPARATORS:
            if seen_decimal_separator:
                continue
            normalized_characters.append(".")
            seen_decimal_separator = True
            continue
        try:
            normalized_characters.append(str(unicodedata.decimal(character)))
        except (TypeError, ValueError):
            normalized_characters.append(character)

    normalized = "".join(normalized_characters)
    if normalized.count(".") > 1:
        raise InvalidOperation
    return normalized


def _normalize_integer_input(raw_value: str) -> str:
    normalized_characters: list[str] = []

    for character in raw_value.strip():
        if character in {"\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u2066", "\u2067", "\u2069"}:
            continue
        if character.isspace() or character in DECIMAL_SEPARATORS:
            continue
        try:
            normalized_characters.append(str(unicodedata.decimal(character)))
        except (TypeError, ValueError):
            normalized_characters.append(character)

    return "".join(normalized_characters)
