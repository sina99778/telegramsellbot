from __future__ import annotations

import base64
import logging
from decimal import Decimal, InvalidOperation
import unicodedata
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, not_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.keyboards.inline import add_pagination_controls
from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import CreatePlanStates, PlanEditStates
from core.formatting import format_volume_bytes
from core.texts import AdminButtons, AdminMessages, Buttons
from models.plan import Plan
from models.user import User
from models.xui import XUIInboundRecord
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from apps.bot.utils.button_style import strip_leading_emoji
from apps.bot.utils.messaging import safe_edit_or_send
from apps.bot.utils.panels import admin_panel, status_label
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


_MENU_INTERRUPT_LABELS = (
    Buttons.BUY_CONFIG,
    Buttons.PROFILE_WALLET,
    Buttons.SUPPORT,
    Buttons.MY_CONFIGS,
)
# Include the emoji-stripped variants too, so the interrupt still fires when
# premium-emoji icons removed the leading emoji from a menu button's text.
MENU_INTERRUPT_TEXTS = set(_MENU_INTERRUPT_LABELS) | {
    strip_leading_emoji(label) for label in _MENU_INTERRUPT_LABELS
}


DECIMAL_SEPARATORS = {".", ",", "\u066b", "\u066c", "\u060c"}


async def _resolve_money_input_mode(session: AsyncSession) -> tuple[str, int]:
    """Return (display_currency, toman_rate). Used by every price prompt.

    Internal storage is always USD; the admin enters values in whatever
    the bot's global display currency is set to, and the handler
    converts to USD before persisting.
    """
    repo = AppSettingsRepository(session)
    return await repo.get_display_currency(), int(await repo.get_toman_rate())


def _money_unit_label(display_currency: str) -> str:
    return "\u062a\u0648\u0645\u0627\u0646" if display_currency == "IRT" else "\u062f\u0644\u0627\u0631"


def _to_usd(amount: Decimal, display_currency: str, toman_rate: int) -> Decimal:
    """Convert admin input to USD. Internal storage is always USD."""
    if display_currency == "IRT" and toman_rate > 0:
        return (amount / Decimal(toman_rate)).quantize(Decimal("0.00000001"))
    return amount


@router.message(Command("cancel"), CreatePlanStates.waiting_for_inbound_selection)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_name)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_duration_days)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_volume_gb)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_price)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_renewal_price_per_gb)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_renewal_price_per_day)
@router.message(Command("cancel"), CreatePlanStates.waiting_for_ip_limit)
@router.message(Command("cancel"), PlanEditStates.waiting_for_duration_days)
@router.message(Command("cancel"), PlanEditStates.waiting_for_name)
@router.message(Command("cancel"), PlanEditStates.waiting_for_price)
@router.message(Command("cancel"), PlanEditStates.waiting_for_stock_limit)
@router.message(Command("cancel"), PlanEditStates.waiting_for_ip_limit)
@router.message(Command("cancel"), PlanEditStates.waiting_for_renewal_price_per_gb)
@router.message(Command("cancel"), PlanEditStates.waiting_for_renewal_price_per_day)
async def cancel_plan_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(AdminMessages.PLAN_CREATION_CANCELLED)


@router.message(CreatePlanStates.waiting_for_inbound_selection, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_name, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_duration_days, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_volume_gb, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_price, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_renewal_price_per_gb, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_renewal_price_per_day, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(CreatePlanStates.waiting_for_ip_limit, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_duration_days, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_name, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_price, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_stock_limit, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_ip_limit, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_renewal_price_per_gb, F.text.in_(MENU_INTERRUPT_TEXTS))
@router.message(PlanEditStates.waiting_for_renewal_price_per_day, F.text.in_(MENU_INTERRUPT_TEXTS))
async def interrupt_plan_creation_with_main_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(AdminMessages.PLAN_CREATION_INTERRUPTED)


@router.callback_query(F.data == "admin:plans")
async def admin_plans_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    text = admin_panel(
        "مدیریت پلن‌ها",
        [
            (
                "عملیات",
                [
                    ("ساخت", "ایجاد پلن فروش جدید"),
                    ("لیست", "ویرایش نام، قیمت، مدت و موجودی"),
                ],
            ),
        ],
    )
    builder = InlineKeyboardBuilder()
    builder.button(text=AdminButtons.CREATE_PLAN, callback_data="admin:plans:create")
    builder.button(text=AdminButtons.LIST_PLANS, callback_data=PlanListPageCallback(page=1).pack())
    builder.button(text=AdminButtons.BACK, callback_data="admin:main")
    builder.adjust(2, 1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(PlanListPageCallback.filter())
async def list_plans(
    callback: CallbackQuery,
    callback_data: PlanListPageCallback,
    session: AsyncSession,
) -> None:
    await callback.answer()
    page = max(callback_data.page, 1)
    
    query = select(Plan).where(
        ~Plan.name.startswith("[حذف شده]"),
        not_(Plan.code.like("custom\\_%", escape="\\")),
    )
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
        text = admin_panel(
            "لیست پلن‌ها",
            [
                (
                    "راهنما",
                    [
                        ("ویرایش", "روی پلن بزنید"),
                        ("صفحه", page),
                    ],
                ),
            ],
        )
        markup = _build_plan_list_keyboard(plans, page=page, total_items=total_plans)

    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        await safe_edit_or_send(callback, text, reply_markup=markup, parse_mode="HTML")


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
    inbound_label = (
        f"{plan.inbound.remark} (ID: {plan.inbound.xui_inbound_remote_id})"
        if plan.inbound
        else "نامشخص"
    )

    # Per-plan override labels — "پیش‌فرض" means "fall back to the global
    # setting in the bot's admin → settings menu".
    ip_limit_label = (
        "نامحدود" if plan.ip_limit == 0 else
        f"{plan.ip_limit} دستگاه" if plan.ip_limit is not None else "پیش‌فرض عمومی"
    )
    renewal_gb_label = (
        f"{plan.renewal_price_per_gb} {plan.currency} / GB"
        if plan.renewal_price_per_gb is not None else "پیش‌فرض عمومی"
    )
    renewal_day_label = (
        f"{plan.renewal_price_per_day} {plan.currency} / روز"
        if plan.renewal_price_per_day is not None else "پیش‌فرض عمومی"
    )

    text = admin_panel(
        "جزئیات پلن",
        [
            (
                "مشخصات",
                [
                    ("نام", plan.name),
                    ("پروتکل", plan.protocol),
                    ("اینباند", inbound_label),
                    ("وضعیت", status_label(plan.is_active)),
                ],
            ),
            (
                "فروش",
                [
                    ("مدت", f"{plan.duration_days} روز"),
                    ("حجم", format_volume_bytes(plan.volume_bytes)),
                    ("موجودی", stock_label),
                    ("قیمت", f"{plan.price} {plan.currency}"),
                ],
            ),
            (
                "محدودیت‌ها و قیمت تمدید",
                [
                    ("محدودیت IP", ip_limit_label),
                    ("تمدید هر گیگ", renewal_gb_label),
                    ("تمدید هر روز", renewal_day_label),
                ],
            ),
        ],
    )

    builder = InlineKeyboardBuilder()
    status_toggle_text = "🔴 غیرفعال کردن" if plan.is_active else "🟢 فعال کردن"
    builder.button(
        text=status_toggle_text,
        callback_data=PlanActionCallback(action="toggle", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="✏️ تغییر نام",
        callback_data=PlanActionCallback(action="edit_name", plan_id=plan.id, page=callback_data.page).pack(),
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
        text="🔐 محدودیت IP",
        callback_data=PlanActionCallback(action="edit_ip_limit", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="📦 قیمت تمدید گیگ",
        callback_data=PlanActionCallback(action="edit_renew_gb", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="⏳ قیمت تمدید روز",
        callback_data=PlanActionCallback(action="edit_renew_day", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="✖ حذف پلن",
        callback_data=PlanActionCallback(action="delete", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="🔙 بازگشت به لیست",
        callback_data=PlanListPageCallback(page=callback_data.page).pack(),
    )
    builder.adjust(2, 2, 2, 2, 1, 1)
    
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


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
        meta = inbound.metadata_ or {}
        proto = (inbound.protocol or "").lower()
        # Marzban-family bundle (PasarGuard group / Rebecca service). The
        # pasarguard_group key is kept for rows synced before the rename.
        if proto in ("pasarguard", "rebecca") or meta.get("marzban_bundle") or meta.get("pasarguard_group"):
            if proto == "rebecca":
                label = f"🟪 Rebecca | سرویس: {inbound.remark or 'بدون نام'} | سرور: {server_name}"
            else:
                label = f"🟩 PasarGuard | گروه: {inbound.remark or 'بدون نام'} | سرور: {server_name}"
        else:
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
async def create_plan_volume(message: Message, state: FSMContext, session: AsyncSession) -> None:
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

    # Make the price prompt match the bot's global display-currency mode
    # so the admin types in the unit they think in.
    display_currency, _rate = await _resolve_money_input_mode(session)
    unit = _money_unit_label(display_currency)
    await message.answer(f"💰 قیمت فروش این پلن را به {unit} وارد کنید.")


@router.message(CreatePlanStates.waiting_for_price)
async def create_plan_price(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Collect the sale price, then chain into the two REQUIRED renewal-pricing
    questions (per-GB and per-day) before the optional ip_limit step.

    Per-plan renewal pricing replaces the old global price_per_gb /
    price_per_10_days. The global setting still acts as a backstop for
    legacy plans that have NULL overrides.
    """
    if not message.text:
        return

    try:
        price_input = Decimal(_normalize_decimal_input(message.text))
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return

    if price_input <= Decimal("0"):
        await message.answer(AdminMessages.PRICE_GT_ZERO)
        return

    display_currency, toman_rate = await _resolve_money_input_mode(session)
    price_usd = _to_usd(price_input, display_currency, toman_rate)

    # Store as USD; remember both the input + the resolved display mode so
    # subsequent steps don't have to look it up again.
    await state.update_data(
        price=str(price_usd),
        display_currency=display_currency,
        toman_rate=toman_rate,
    )
    await state.set_state(CreatePlanStates.waiting_for_renewal_price_per_gb)
    unit = _money_unit_label(display_currency)
    await message.answer(
        f"💰 قیمت تمدید این پلن برای <b>هر گیگابایت</b> اضافه را به {unit} وارد کنید.\n\n"
        "این قیمت مخصوص همین پلن است (به‌جای تنظیم عمومی قبلی).\n"
        "برای لغو /cancel را بزنید.",
        parse_mode="HTML",
    )


@router.message(CreatePlanStates.waiting_for_renewal_price_per_gb)
async def create_plan_renewal_per_gb(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return
    try:
        gb_input = Decimal(_normalize_decimal_input(message.text))
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return
    if gb_input < Decimal("0"):
        await message.answer("❌ قیمت نمی‌تواند منفی باشد.")
        return

    data = await state.get_data()
    display_currency = str(data.get("display_currency", "USD"))
    toman_rate = int(data.get("toman_rate", 100000))
    per_gb_usd = _to_usd(gb_input, display_currency, toman_rate)

    await state.update_data(renewal_price_per_gb=str(per_gb_usd))
    await state.set_state(CreatePlanStates.waiting_for_renewal_price_per_day)
    unit = _money_unit_label(display_currency)
    await message.answer(
        f"⏳ قیمت تمدید این پلن برای <b>هر روز</b> اضافه را به {unit} وارد کنید.\n\n"
        "برای لغو /cancel را بزنید.",
        parse_mode="HTML",
    )


@router.message(CreatePlanStates.waiting_for_renewal_price_per_day)
async def create_plan_renewal_per_day(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if not message.text:
        return
    try:
        day_input = Decimal(_normalize_decimal_input(message.text))
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return
    if day_input < Decimal("0"):
        await message.answer("❌ قیمت نمی‌تواند منفی باشد.")
        return

    data = await state.get_data()
    display_currency = str(data.get("display_currency", "USD"))
    toman_rate = int(data.get("toman_rate", 100000))
    per_day_usd = _to_usd(day_input, display_currency, toman_rate)

    await state.update_data(renewal_price_per_day=str(per_day_usd))
    await state.set_state(CreatePlanStates.waiting_for_ip_limit)
    await message.answer(
        "🔐 محدودیت آی‌پی برای این پلن را وارد کنید (تعداد دستگاه هم‌زمان):\n"
        "• یک عدد ≥ ۱  → همان مقدار به X-UI داده می‌شود\n"
        "• <code>۰</code>  → نامحدود (طبق قرارداد X-UI)\n"
        "• <code>-</code>  → از پیش‌فرض عمومی استفاده شود\n\n"
        "برای لغو /cancel را بزنید.",
        parse_mode="HTML",
    )


@router.message(CreatePlanStates.waiting_for_ip_limit)
async def create_plan_ip_limit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    """Finalize plan creation with the optional ip_limit override."""
    if not message.text:
        return

    raw = message.text.strip()
    ip_limit: int | None
    if raw in {"-", "_", "—", "none", "None"}:
        ip_limit = None
    else:
        try:
            ip_limit = int(_normalize_integer_input(raw))
            if ip_limit < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ مقدار نامعتبر. عدد ≥ ۰ یا کاراکتر «-» را بفرستید.")
            return

    form_data = await state.get_data()
    volume_bytes = int(form_data["volume_gb"]) * 1024 * 1024 * 1024
    protocol = str(form_data["protocol"])
    inbound_id = UUID(str(form_data["inbound_id"]))
    price = Decimal(str(form_data["price"]))
    renewal_price_per_gb = (
        Decimal(str(form_data["renewal_price_per_gb"]))
        if form_data.get("renewal_price_per_gb") is not None
        else None
    )
    renewal_price_per_day = (
        Decimal(str(form_data["renewal_price_per_day"]))
        if form_data.get("renewal_price_per_day") is not None
        else None
    )
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
        ip_limit=ip_limit,
        renewal_price_per_gb=renewal_price_per_gb,
        renewal_price_per_day=renewal_price_per_day,
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


@router.callback_query(PlanActionCallback.filter(F.action == "edit_name"))
async def edit_plan_name_start(
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
    await state.set_state(PlanEditStates.waiting_for_name)
    await safe_edit_or_send(
        callback,
        f"نام فعلی پلن «{plan.name}» است.\nنام جدید را ارسال کنید. برای لغو /cancel را بزنید.",
    )


@router.message(PlanEditStates.waiting_for_name)
async def edit_plan_name_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return
    name = message.text.strip()
    if not name or len(name) > 128:
        await message.answer("نام پلن باید بین ۱ تا ۱۲۸ کاراکتر باشد.")
        return
    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    old_name = plan.name
    plan.name = name
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="edit_plan_name",
        entity_type="plan",
        entity_id=plan.id,
        payload={"from": old_name, "to": name},
    )
    await session.flush()
    await message.answer(f"✅ نام پلن از «{old_name}» به «{name}» تغییر کرد.")


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
    display_currency, toman_rate = await _resolve_money_input_mode(session)
    unit = _money_unit_label(display_currency)
    current_display = int(plan.price * toman_rate) if display_currency == "IRT" else float(plan.price)
    await state.update_data(
        plan_id=str(plan.id), page=callback_data.page,
        display_currency=display_currency, toman_rate=toman_rate,
    )
    await state.set_state(PlanEditStates.waiting_for_price)
    await safe_edit_or_send(
        callback,
        f"قیمت فعلی پلن «{plan.name}»: {current_display} {unit}\n"
        f"قیمت جدید را به {unit} ارسال کنید. برای لغو /cancel را بزنید.",
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
        price_input = Decimal(_normalize_decimal_input(message.text))
    except InvalidOperation:
        await message.answer(AdminMessages.INVALID_PRICE)
        return
    if price_input <= Decimal("0"):
        await message.answer(AdminMessages.PRICE_GT_ZERO)
        return
    data = await state.get_data()
    # Convert from whatever currency the admin was prompted in back to USD
    display_currency = str(data.get("display_currency", "USD"))
    toman_rate = int(data.get("toman_rate", 100000))
    price = _to_usd(price_input, display_currency, toman_rate)
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


# ─── Per-plan overrides: IP limit, renewal/GB, renewal/day ──────────────
# All three follow the same input convention:
#   • a positive number       → set as override
#   • 0                       → 0 (unlimited for ip_limit, free for prices)
#   • "-" / "_" / "—" / "none"→ clear override → fall back to global default


@router.callback_query(PlanActionCallback.filter(F.action == "edit_ip_limit"))
async def edit_plan_ip_limit_start(
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
    current = (
        "نامحدود (۰)" if plan.ip_limit == 0 else
        str(plan.ip_limit) if plan.ip_limit is not None else "پیش‌فرض عمومی"
    )
    await state.update_data(plan_id=str(plan.id), page=callback_data.page)
    await state.set_state(PlanEditStates.waiting_for_ip_limit)
    await safe_edit_or_send(
        callback,
        f"🔐 محدودیت آی‌پی فعلی پلن «{plan.name}»: {current}\n\n"
        "مقدار جدید را بفرستید:\n"
        "• یک عدد ≥ ۱ → همان مقدار\n"
        "• <code>۰</code> → نامحدود\n"
        "• <code>-</code> → استفاده از پیش‌فرض عمومی\n\n"
        "برای لغو /cancel را بزنید.",
        parse_mode="HTML",
    )


@router.message(PlanEditStates.waiting_for_ip_limit)
async def edit_plan_ip_limit_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    if not message.text:
        return
    raw = message.text.strip()
    if raw in {"-", "_", "—", "none", "None"}:
        new_value: int | None = None
    else:
        try:
            new_value = int(_normalize_integer_input(raw))
            if new_value < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ مقدار نامعتبر. عدد ≥ ۰ یا «-» را بفرستید.")
            return

    data = await state.get_data()
    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    old_value = plan.ip_limit
    plan.ip_limit = new_value
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action="edit_plan_ip_limit",
            entity_type="plan",
            entity_id=plan.id,
            payload={"from": old_value, "to": new_value},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.flush()
    label = "پیش‌فرض عمومی" if new_value is None else ("نامحدود" if new_value == 0 else str(new_value))
    await message.answer(f"✅ محدودیت IP پلن «{plan.name}» تنظیم شد: {label}")


async def _edit_renewal_price_start(
    *,
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
    unit_label: str,
    target_state: State,
    current_value: Decimal | None,
    plan_name: str,
) -> None:
    await callback.answer()
    display_currency, toman_rate = await _resolve_money_input_mode(session)
    money_unit = _money_unit_label(display_currency)
    if current_value is None:
        current = "پیش‌فرض عمومی"
    elif display_currency == "IRT":
        current = f"{int(current_value * toman_rate):,} {money_unit}"
    else:
        current = f"{current_value} {money_unit}"
    await state.update_data(
        plan_id=str(callback_data.plan_id),
        page=callback_data.page,
        display_currency=display_currency,
        toman_rate=toman_rate,
    )
    await state.set_state(target_state)
    await safe_edit_or_send(
        callback,
        f"💰 قیمت تمدید هر {unit_label} برای پلن «{plan_name}»\n"
        f"مقدار فعلی: {current}\n\n"
        f"مقدار جدید را به {money_unit} بفرستید:\n"
        "• یک عدد (مثلاً 0.5)\n"
        "• <code>-</code> → استفاده از پیش‌فرض عمومی\n\n"
        "برای لغو /cancel را بزنید.",
        parse_mode="HTML",
    )


@router.callback_query(PlanActionCallback.filter(F.action == "edit_renew_gb"))
async def edit_plan_renewal_gb_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await callback.answer()
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return
    await _edit_renewal_price_start(
        callback=callback, callback_data=callback_data, state=state, session=session,
        unit_label="گیگابایت", target_state=PlanEditStates.waiting_for_renewal_price_per_gb,
        current_value=plan.renewal_price_per_gb, plan_name=plan.name,
    )


@router.callback_query(PlanActionCallback.filter(F.action == "edit_renew_day"))
async def edit_plan_renewal_day_start(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await callback.answer()
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return
    await _edit_renewal_price_start(
        callback=callback, callback_data=callback_data, state=state, session=session,
        unit_label="روز", target_state=PlanEditStates.waiting_for_renewal_price_per_day,
        current_value=plan.renewal_price_per_day, plan_name=plan.name,
    )


async def _submit_renewal_price_override(
    *,
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
    field: str,
    unit_label: str,
) -> None:
    raw = message.text.strip() if message.text else ""
    if not raw:
        return
    data = await state.get_data()
    display_currency = str(data.get("display_currency", "USD"))
    toman_rate = int(data.get("toman_rate", 100000))
    if raw in {"-", "_", "—", "none", "None"}:
        new_value: Decimal | None = None
    else:
        try:
            new_value_input = Decimal(_normalize_decimal_input(raw))
            if new_value_input < 0:
                raise InvalidOperation
        except InvalidOperation:
            await message.answer("❌ مقدار نامعتبر. عدد ≥ ۰ یا «-» را بفرستید.")
            return
        # Convert from whatever display currency the admin typed in → USD
        new_value = _to_usd(new_value_input, display_currency, toman_rate)

    await state.clear()
    plan = await session.get(Plan, UUID(str(data["plan_id"])))
    if plan is None:
        await message.answer(AdminMessages.PLAN_NOT_FOUND)
        return
    old_value = getattr(plan, field)
    setattr(plan, field, new_value)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=admin_user.id,
            action=f"edit_plan_{field}",
            entity_type="plan",
            entity_id=plan.id,
            payload={"from": str(old_value) if old_value is not None else None,
                     "to": str(new_value) if new_value is not None else None},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.flush()
    label = "پیش‌فرض عمومی" if new_value is None else f"{new_value} {plan.currency} / {unit_label}"
    await message.answer(f"✅ قیمت تمدید هر {unit_label} پلن «{plan.name}» تنظیم شد: {label}")


@router.message(PlanEditStates.waiting_for_renewal_price_per_gb)
async def edit_plan_renewal_gb_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await _submit_renewal_price_override(
        message=message, state=state, session=session, admin_user=admin_user,
        field="renewal_price_per_gb", unit_label="گیگابایت",
    )


@router.message(PlanEditStates.waiting_for_renewal_price_per_day)
async def edit_plan_renewal_day_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    await _submit_renewal_price_override(
        message=message, state=state, session=session, admin_user=admin_user,
        field="renewal_price_per_day", unit_label="روز",
    )


@router.callback_query(PlanActionCallback.filter(F.action == "delete"))
async def delete_plan_confirm(
    callback: CallbackQuery,
    callback_data: PlanActionCallback,
    session: AsyncSession,
) -> None:
    """Show a confirmation prompt with a sales-count diff before deletion."""
    await callback.answer()
    plan = await session.get(Plan, callback_data.plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    from sqlalchemy import func as _func
    from models.order import Order
    sold_count = int(
        await session.scalar(
            select(_func.count()).select_from(Order).where(
                Order.plan_id == plan.id,
                Order.status.in_(("provisioned", "paid", "completed")),
            )
        ) or 0
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="❗️ بله، حذف کن",
        callback_data=PlanActionCallback(action="del_ok", plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.button(
        text="↩️ انصراف",
        callback_data=ViewPlanCallback(plan_id=plan.id, page=callback_data.page).pack(),
    )
    builder.adjust(1)

    warning = (
        f"⚠️ <b>تأیید حذف پلن</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"نام پلن: <b>{plan.name}</b>\n"
        f"تعداد فروش انجام‌شده: <b>{sold_count}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        "این عمل غیرقابل بازگشت است.\n"
        "اگر پلن دارای کاربر فعال باشد، به جای حذف، غیرفعال خواهد شد."
    )
    await safe_edit_or_send(callback, warning, reply_markup=builder.as_markup())


@router.callback_query(PlanActionCallback.filter(F.action == "del_ok"))
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


def _pack_uuid(value: UUID) -> str:
    """UUID -> 22-char urlsafe-base64 (no padding) so two ids fit in callback data."""
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def _unpack_uuid(value: str) -> UUID:
    """Inverse of _pack_uuid. Raises ValueError on malformed input."""
    try:
        return UUID(bytes=base64.urlsafe_b64decode(value + "=="))
    except Exception as exc:  # binascii.Error / ValueError on bad input
        raise ValueError(f"bad packed uuid: {value!r}") from exc


class ChangeInboundCallback(CallbackData, prefix="chinb"):
    # Telegram caps callback data at 64 bytes — two str(UUID)s don't fit, so
    # both ids travel as 22-char packed base64. Carrying the TARGET PLAN id
    # here (instead of a shared FSM key) makes a stale picker keyboard act on
    # the plan it was rendered for, not whichever plan was opened last.
    inbound_id: str
    plan_id: str
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
                inbound_id=_pack_uuid(inb.id),
                plan_id=_pack_uuid(plan.id),
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

    # Both ids travel packed in the callback data, so the action always
    # targets the plan this picker was rendered for (no shared FSM key).
    try:
        target_plan_id = _unpack_uuid(callback_data.plan_id)
        target_inbound_id = _unpack_uuid(callback_data.inbound_id)
    except ValueError:
        await safe_edit_or_send(callback, "\u274c \u0627\u0637\u0644\u0627\u0639\u0627\u062a \u067e\u0644\u0646 \u06cc\u0627\u0641\u062a \u0646\u0634\u062f. \u062f\u0648\u0628\u0627\u0631\u0647 \u062a\u0644\u0627\u0634 \u06a9\u0646\u06cc\u062f.")
        return
    page = callback_data.page

    plan = await session.get(Plan, target_plan_id)
    if plan is None:
        await safe_edit_or_send(callback, AdminMessages.PLAN_NOT_FOUND)
        return

    new_inbound = await session.scalar(
        select(XUIInboundRecord)
        .options(selectinload(XUIInboundRecord.server))
        .where(XUIInboundRecord.id == target_inbound_id)
    )
    if new_inbound is None:
        await safe_edit_or_send(callback, "\u274c \u0627\u06cc\u0646\u0628\u0627\u0646\u062f \u067e\u06cc\u062f\u0627 \u0646\u0634\u062f.")
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

    server_name = new_inbound.server.name if new_inbound.server else "\u0646\u0627\u0645\u0634\u062e\u0635"
    await callback.answer(f"\u2705 \u0633\u0631\u0648\u0631 \u067e\u0644\u0646 \u0628\u0647 {server_name} \u062a\u063a\u06cc\u06cc\u0631 \u06cc\u0627\u0641\u062a.", show_alert=True)
    await view_plan(callback, ViewPlanCallback(plan_id=plan.id, page=page), session)


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
