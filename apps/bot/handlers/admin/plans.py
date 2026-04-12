from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import CreatePlanStates
from core.formatting import format_volume_bytes
from core.texts import AdminButtons, AdminMessages, Common
from models.plan import Plan
from models.user import User
from models.xui import XUIInboundRecord
from repositories.audit import AuditLogRepository


router = Router(name="admin-plans")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

PLAN_PAGE_SIZE = 5


class PlanActionCallback(CallbackData, prefix="plan_admin"):
    action: str
    plan_id: UUID
    page: int = 1


class PlanListPageCallback(CallbackData, prefix="plan_list"):
    page: int


class InboundSelectCallback(CallbackData, prefix="inbound_sel"):
    inbound_id: UUID


@router.callback_query(F.data == "admin:plans")
async def admin_plans_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.CREATE_PLAN, callback_data="admin:plans:create")
    builder.button(text=AdminButtons.LIST_PLANS, callback_data=PlanListPageCallback(page=1).pack())
    builder.adjust(1)
    await callback.message.answer(AdminMessages.PLAN_MANAGEMENT, reply_markup=builder.as_markup())


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
            [
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
            ]
        )
        markup = _build_plan_list_keyboard(plans, page=page, total_items=total_plans)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin:plans:create")
async def create_plan_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()

    # Show available inbounds for the admin to select
    result = await session.execute(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.is_active.is_(True))
        .order_by(XUIInboundRecord.created_at.asc())
    )
    inbounds = list(result.scalars().all())

    if not inbounds:
        await callback.message.answer(
            "❌ هیچ اینباند فعالی موجود نیست.\n"
            "ابتدا یک سرور اضافه کنید تا اینباندها از پنل دریافت شوند."
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
    await callback.message.answer(
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
        .where(XUIInboundRecord.id == callback_data.inbound_id)
    )
    if inbound is None:
        await callback.message.answer("اینباند پیدا نشد.")
        await state.clear()
        return

    await state.update_data(
        inbound_id=str(inbound.id),
        protocol=inbound.protocol or "unknown",
        inbound_remote_id=inbound.xui_inbound_remote_id,
    )
    await state.set_state(CreatePlanStates.waiting_for_name)
    await callback.message.answer(AdminMessages.ENTER_PLAN_NAME)


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
        duration_days = int(message.text.strip())
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
        volume_gb = int(message.text.strip())
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
        price = Decimal(message.text.strip())
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    if price < Decimal("0"):
        await message.answer(AdminMessages.PRICE_GT_ZERO)
        return

    form_data = await state.get_data()
    volume_bytes = int(form_data["volume_gb"]) * 1024 * 1024 * 1024
    protocol = str(form_data["protocol"])
    inbound_id = UUID(str(form_data["inbound_id"]))
    code = (
        f"{protocol}_{int(form_data['duration_days'])}d_"
        f"{int(form_data['volume_gb'])}gb_{price.normalize()}"
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
    session.add(plan)
    await session.flush()
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

    await state.clear()
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
        await callback.message.answer(AdminMessages.PLAN_NOT_FOUND)
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
    builder.adjust(1)
    add_pagination_controls(
        builder,
        page=page,
        total_items=total_items,
        page_size=PLAN_PAGE_SIZE,
        prev_callback_data=PlanListPageCallback(page=page - 1).pack(),
        next_callback_data=PlanListPageCallback(page=page + 1).pack(),
    )
    return builder.as_markup()
