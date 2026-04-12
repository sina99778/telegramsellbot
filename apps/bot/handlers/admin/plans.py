from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import CreatePlanStates
from models.plan import Plan
from models.user import User
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


@router.callback_query(F.data == "admin:plans")
async def admin_plans_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="Create Plan", callback_data="admin:plans:create")
    builder.button(text="List Plans", callback_data=PlanListPageCallback(page=1).pack())
    builder.adjust(1)
    await callback.message.answer("Plan management:", reply_markup=builder.as_markup())


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
        .order_by(Plan.created_at.asc())
        .offset((page - 1) * PLAN_PAGE_SIZE)
        .limit(PLAN_PAGE_SIZE)
    )
    plans = list(result.scalars().all())

    if not plans:
        text = "No plans are configured yet."
        markup = None
    else:
        text = "\n\n".join(
            [
                (
                    f"Plan: {plan.name}\n"
                    f"Protocol: {plan.protocol}\n"
                    f"Duration: {plan.duration_days} days\n"
                    f"Volume: {plan.volume_bytes} bytes\n"
                    f"Price: {plan.price} {plan.currency}\n"
                    f"Status: {'active' if plan.is_active else 'inactive'}"
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
async def create_plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(CreatePlanStates.waiting_for_name)
    await callback.message.answer("Enter the plan name.")


@router.message(CreatePlanStates.waiting_for_name)
async def create_plan_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(CreatePlanStates.waiting_for_protocol)
    await message.answer("Enter the protocol: `vless` or `vmess`.")


@router.message(CreatePlanStates.waiting_for_protocol)
async def create_plan_protocol(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    protocol = message.text.strip().lower()
    if protocol not in {"vless", "vmess"}:
        await message.answer("Protocol must be `vless` or `vmess`.")
        return
    await state.update_data(protocol=protocol)
    await state.set_state(CreatePlanStates.waiting_for_duration_days)
    await message.answer("Enter the duration in days.")


@router.message(CreatePlanStates.waiting_for_duration_days)
async def create_plan_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        duration_days = int(message.text.strip())
    except ValueError:
        await message.answer("Please enter a valid integer number of days.")
        return
    if duration_days <= 0:
        await message.answer("Duration must be greater than zero.")
        return
    await state.update_data(duration_days=duration_days)
    await state.set_state(CreatePlanStates.waiting_for_volume_gb)
    await message.answer("Enter the volume in GB.")


@router.message(CreatePlanStates.waiting_for_volume_gb)
async def create_plan_volume(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        volume_gb = int(message.text.strip())
    except ValueError:
        await message.answer("Please enter a valid integer number of GB.")
        return
    if volume_gb <= 0:
        await message.answer("Volume must be greater than zero.")
        return
    await state.update_data(volume_gb=volume_gb)
    await state.set_state(CreatePlanStates.waiting_for_price)
    await message.answer("Enter the price in USD.")


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
        await message.answer("Please enter a valid decimal price.")
        return

    if price <= Decimal("0"):
        await message.answer("Price must be greater than zero.")
        return

    form_data = await state.get_data()
    volume_bytes = int(form_data["volume_gb"]) * 1024 * 1024 * 1024
    code = f"{str(form_data['protocol'])}_{int(form_data['duration_days'])}d_{int(form_data['volume_gb'])}gb_{price.normalize()}"

    plan = Plan(
        code=code,
        name=str(form_data["name"]),
        protocol=str(form_data["protocol"]),
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
        payload={"code": plan.code, "price": str(plan.price)},
    )

    await state.clear()
    await message.answer(f"Plan `{plan.name}` created successfully and is now available for purchase.")


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
        await callback.message.answer("Plan not found.")
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
) -> InlineKeyboardBuilder:
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
    return builder
