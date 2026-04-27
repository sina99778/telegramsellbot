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
from apps.bot.states.admin import CreatePlanStates, PlanEditStates
from core.formatting import format_volume_bytes
from core.texts import AdminButtons, AdminMessages, Buttons, Common
from models.plan import Plan
from models.user import User
from models.xui import XUIInboundRecord
from repositories.audit import AuditLogRepository
from apps.bot.utils.messaging import safe_edit_or_send
from services.plan_inventory import get_plan_stock_map, set_plan_sales_limit


router = Router(name="admin-plans")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

logger = logging.getLogger(__name__)

PLAN_PAGE_SIZE = 5


class PlanActionCallback(CallbackData, prefix="plan_admin"):
    action: str
    plan_id: UUID
    page: int = 1


class ViewPlanCallback(CallbackData, prefix="plan_view"):
    plan_id: UUID
    page: int


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
@router.message(Command("cancel"), PlanEditStates.waiting_for_duration_days)
@router.message(Command("cancel"), PlanEditStates.waiting_for_price)
@router.message(Command("cancel"), PlanEditStates.waiting_for_stock_limit)
async def cancel_plan_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(AdminMessages.PLAN_CREATION_CANCELLED)


@router.message(CreatePlanStates.waiting_for_inbound_selection, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_name, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_duration_days, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_volume_gb, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_price, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_duration_days, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_price, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_stock_limit, F.text.in_(MENU_INTERRUPT_TEXTS))
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
    
    query = select(Plan).where(~Plan.name.startswith("[حذف شده]"))
    total_plans = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    
    result = await session.execute(
        query
        .order_by(Plan.created_at.asc())
        .offset((page - 1) * PLAN_PAGE_SIZE)
        .limit(PLAN_PAGE_SIZE)
    )
    plans = list(result.scalars().all())

    if not plans:
        text = AdminMessages.NO_PLANS
        markup = None
    else:
        text = "📦 **لیست پلن‌های تعریف شده:**\nبرای مشاهده جزئیات یا ویرایش، روی پلن مورد نظر کلیک کنید."
        markup = _build_plan_list_keyboard(plans, page=page, total_items=total_plans)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await safe_edit_or_send(callback, text, reply_markup=markup)


@router.callback_query(ViewPlanCallback.filter())
async def view_plan(
    callback: CallbackQuery,
    callback_data: ViewPlanCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    plan = await session.scalar(
        select(Plan)
        .options(selectinload(Plan.inbound))
        .where(Plan.id == callback_data.plan_id)
    )
    if not plan:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    stock_state = (await get_plan_stock_map(session, [plan.id]))[plan.id]
    stock_label = "نامحدود" if stock_state.is_unlimited else f"{stock_state.stock_remaining} از {stock_state.sales_limit}"
    text = (
        f"📦 **جزئیات پلن:**\n\n"
        f"🏷 **نام:** {plan.name}\n"
        f"🛡 **پروتکل:** {plan.protocol}\n"
        f"📡 **اینباند:** {plan.inbound.remark if plan.inbound else 'نامشخص'} "
        f"(ID: {plan.inbound.xui_inbound_remote_id if plan.inbound else '-'})\n"
        f"⏳ **مدت:** {plan.duration_days} روز\n"
        f"💾 **حجم:** {format_volume_bytes(plan.volume_bytes)}\n"
        f"📦 **موجودی فروش:** {stock_label}\n"
        f"💲 **قیمت:** {plan.price} {plan.currency}\n"
        f"وضعیت: {Common.ACTIVE if plan.is_active else Common.INACTIVE}\n"
    )

    builder = InlineKeyboardBuilder()
    status_toggle_text = "🔴 غیرفعال کردن" if plan.is_active else "🟢 فعال کردن"
    builder.button(
        text=status_toggle_text,
        callback_data=PlanActionCallback(action="toggle", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="🔗 تغییر سرور",
        callback_data=PlanActionCallback(action="change_inbound", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="⏳ تغییر مدت",
        callback_data=PlanActionCallback(action="edit_duration", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="💲 تغییر قیمت",
        callback_data=PlanActionCallback(action="edit_price", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="📦 تنظیم موجودی فروش",
        callback_data=PlanActionCallback(action="edit_stock", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="✖ حذف پلن",
        callback_data=PlanActionCallback(action="delete", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="🔙 بازگشت به لیست",
        callback_data=PlanListPageCallback(page=callback_data.page).pack(),
    )
    builder.adjust(1)
    
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


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
    await view_plan(callback, ViewPlanCallback(plan_id=plan.id, page=callback_data.page), session)


@router.callback_query(PlanActionCallback.filter(F.action == "edit_duration"))
async def edit_plan_duration_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return
    await state.update_data(plan_id=str(plan.id), page=callback_data.page)
    await state.set_state(PlanEditStates.waiting_for_duration_days)
    await safe_edit_or_send(
        callback,
        f"مدت فعلی پلن «{plan.name}»: {plan.duration_days} روز\n"
        "مدت جدید را به روز ارسال کنید. برای لغو /cancel را بزنید.",
    )


@router.message(PlanEditStates.waiting_for_duration_days)
async def edit_plan_duration_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
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
    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    old_duration = plan.duration_days
    plan.duration_days = duration_days
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_plan_duration",
        entity_type="plan",
        entity_id=plan.id,
        payload={"from": old_duration, "to": duration_days},
    )
    await session.flush()
    await message.answer(f"✅ مدت پلن «{plan.name}» به {duration_days} روز تغییر کرد.")


@router.callback_query(PlanActionCallback.filter(F.action == "edit_price"))
async def edit_plan_price_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return
    await state.update_data(plan_id=str(plan.id), page=callback_data.page)
    await state.set_state(PlanEditStates.waiting_for_price)
    await safe_edit_or_send(
        callback,
        f"قیمت فعلی پلن «{plan.name}»: {plan.price} {plan.currency}\n"
        "قیمت جدید را به دلار ارسال کنید. برای لغو /cancel را بزنید.",
    )


@router.message(PlanEditStates.waiting_for_price)
async def edit_plan_price_submit(
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
    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    old_price = plan.price
    old_renewal_price = plan.renewal_price
    plan.price = price
    plan.renewal_price = price
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_plan_price",
        entity_type="plan",
        entity_id=plan.id,
        payload={
            "from": str(old_price),
            "to": str(price),
            "renewal_price_from": str(old_renewal_price),
            "renewal_price_to": str(price),
        },
    )
    await session.flush()
    await message.answer(f"✅ قیمت پلن «{plan.name}» به {price} {plan.currency} تغییر کرد.")


@router.callback_query(PlanActionCallback.filter(F.action == "edit_stock"))
async def edit_plan_stock_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return
    stock_state = (await get_plan_stock_map(session, [plan.id]))[plan.id]
    current = "نامحدود" if stock_state.is_unlimited else f"{stock_state.stock_remaining} از {stock_state.sales_limit}"
    await state.update_data(plan_id=str(plan.id), page=callback_data.page)
    await state.set_state(PlanEditStates.waiting_for_stock_limit)
    await safe_edit_or_send(
        callback,
        f"موجودی فعلی پلن «{plan.name}»: {current}\n"
        "حداکثر تعداد فروش را ارسال کنید. عدد 0 یعنی نامحدود و به کاربر نمایش داده نمی‌شود.\n"
        "برای لغو /cancel را بزنید.",
    )


@router.message(PlanEditStates.waiting_for_stock_limit)
async def edit_plan_stock_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return
    try:
        sales_limit = int(_normalize_integer_input(message.text))
    except ValueError:
        await message.answer(AdminMessages.INVALID_INTEGER)
        return
    if sales_limit < 0:
        await message.answer("موجودی باید صفر یا بیشتر باشد.")
        return
    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    stock = await set_plan_sales_limit(session, plan.id, sales_limit)
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_plan_stock",
        entity_type="plan",
        entity_id=plan.id,
        payload={
            "sales_limit": stock.sales_limit,
            "sold_count": stock.sold_count,
            "stock_remaining": stock.stock_remaining,
        },
    )
    await session.flush()
    label = "نامحدود" if stock.is_unlimited else f"{stock.stock_remaining} باقی‌مانده از {stock.sales_limit}"
    await message.answer(f"✅ موجودی فروش پلن «{plan.name}» تنظیم شد: {label}")


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
        await callback.answer("✅ پلن با موفقیت حذف شد.", show_alert=True)
    except IntegrityError:
        await session.rollback()
        plan_again = await session.get(Plan, callback_data.plan_id)
        if plan_again:
            plan_again.is_active = False
            # Append label up to max length (name is usually String(255))
            prefix = "[حذف شده] "
            if not plan_again.name.startswith(prefix):
                plan_again.name = f"{prefix}{plan_again.name}"[:250]
            await session.flush()
        await callback.answer("⚠️ پلن متصل به کاربر است و به لیست دکمه‌ها مخفی (غیرفعال) شد.", show_alert=True)
    except Exception as exc:
        logger.error("Failed to delete plan %s: %s", callback_data.plan_id, exc, exc_info=True)
        await callback.answer(f"❌ خطا در حذف پلن: {str(exc)[:50]}", show_alert=True)
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
        callback_data=ViewPlanCallback(plan_id=plan.id, page=callback_data.page).pack(),
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
    await callback.answer(f"✅ سرور پلن به {server_name} تغییر یافت.", show_alert=True)

    await view_plan(callback, ViewPlanCallback(plan_id=plan.id, page=callback_data.page), session)


def _build_plan_list_keyboard(
    plans: list[Plan],
    *,
    page: int,
    total_items: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        status_icon = "🟢" if plan.is_active else "🔴"
        builder.button(
            text=f"{status_icon} {plan.name}",
            callback_data=ViewPlanCallback(plan_id=plan.id, page=page).pack(),
        )
    builder.adjust(1)
    builder.button(text="🔙 بازگشت", callback_data="admin:plans")
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
