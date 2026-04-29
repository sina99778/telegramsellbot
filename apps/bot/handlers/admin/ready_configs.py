from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
import unicodedata
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import ReadyConfigPlanStates, ReadyConfigUploadStates
from apps.bot.utils.messaging import safe_edit_or_send
from core.formatting import format_volume_bytes
from models.plan import Plan
from models.ready_config import ReadyConfigItem, ReadyConfigPool
from models.user import User
from repositories.audit import AuditLogRepository


router = Router(name="admin-ready-configs")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class ReadyPoolCallback(CallbackData, prefix="rcfg"):
    action: str
    pool_id: UUID


DECIMAL_SEPARATORS = {".", ",", "\u066b", "\u066c", "\u060c"}


@router.callback_query(F.data == "admin:ready_configs")
async def ready_configs_menu(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    pools = await _load_pools(session)
    total_available = 0
    total_sold = 0
    rows: list[str] = []
    for pool in pools:
        available, sold = await _pool_counts(session, pool.id)
        total_available += available
        total_sold += sold
        rows.append(f"- {pool.plan.name}: {available} آماده / {sold} فروخته")

    text = (
        "فروش کانفیگ آماده\n\n"
        f"موجودی آماده: {total_available}\n"
        f"تحویل داده شده: {total_sold}\n\n"
        + ("\n".join(rows) if rows else "هنوز پلن آماده‌ای ساخته نشده است.")
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="ساخت پلن آماده", callback_data="ready_configs:create")
    builder.button(text="آپلود فایل کانفیگ", callback_data="ready_configs:upload")
    builder.button(text="لیست موجودی", callback_data="ready_configs:list")
    builder.button(text="بازگشت", callback_data="admin:main")
    builder.adjust(1)
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup())


@router.callback_query(F.data == "ready_configs:create")
async def create_ready_plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ReadyConfigPlanStates.waiting_for_name)
    await safe_edit_or_send(callback, "نام پلن آماده را وارد کنید. برای لغو /cancel را بفرستید.")


@router.message(F.text == "/cancel", ReadyConfigPlanStates.waiting_for_name)
@router.message(F.text == "/cancel", ReadyConfigPlanStates.waiting_for_duration_days)
@router.message(F.text == "/cancel", ReadyConfigPlanStates.waiting_for_volume_gb)
@router.message(F.text == "/cancel", ReadyConfigPlanStates.waiting_for_price)
@router.message(F.text == "/cancel", ReadyConfigUploadStates.waiting_for_file)
async def cancel_ready_config_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("عملیات لغو شد.")


@router.message(ReadyConfigPlanStates.waiting_for_name)
async def create_ready_plan_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(ReadyConfigPlanStates.waiting_for_duration_days)
    await message.answer("مدت پلن را به روز وارد کنید. مثال: 30")


@router.message(ReadyConfigPlanStates.waiting_for_duration_days)
async def create_ready_plan_duration(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        duration_days = int(_normalize_integer_input(message.text))
    except ValueError:
        await message.answer("لطفا یک عدد معتبر وارد کنید.")
        return
    if duration_days <= 0:
        await message.answer("مدت باید بیشتر از صفر باشد.")
        return
    await state.update_data(duration_days=duration_days)
    await state.set_state(ReadyConfigPlanStates.waiting_for_volume_gb)
    await message.answer("حجم پلن را به گیگابایت وارد کنید. مثال: 50")


@router.message(ReadyConfigPlanStates.waiting_for_volume_gb)
async def create_ready_plan_volume(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    try:
        volume_gb = int(_normalize_integer_input(message.text))
    except ValueError:
        await message.answer("لطفا یک عدد معتبر وارد کنید.")
        return
    if volume_gb <= 0:
        await message.answer("حجم باید بیشتر از صفر باشد.")
        return
    await state.update_data(volume_gb=volume_gb)
    await state.set_state(ReadyConfigPlanStates.waiting_for_price)
    await message.answer("قیمت پلن را به دلار وارد کنید. مثال: 3.5")


@router.message(ReadyConfigPlanStates.waiting_for_price)
async def create_ready_plan_price(
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
        await message.answer("قیمت معتبر نیست.")
        return
    if price <= Decimal("0"):
        await message.answer("قیمت باید بیشتر از صفر باشد.")
        return

    data = await state.get_data()
    await state.clear()
    volume_gb = int(data["volume_gb"])
    plan = Plan(
        code=f"ready_{int(data['duration_days'])}d_{volume_gb}gb_{price.normalize()}_{uuid4().hex[:8]}",
        name=str(data["name"]),
        protocol="ready_config",
        inbound_id=None,
        duration_days=int(data["duration_days"]),
        volume_bytes=volume_gb * 1024 * 1024 * 1024,
        price=price,
        renewal_price=price,
        currency="USD",
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    pool = ReadyConfigPool(plan_id=plan.id, is_active=True)
    session.add(pool)
    await session.flush()
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="create_ready_config_plan",
        entity_type="plan",
        entity_id=plan.id,
        payload={"pool_id": str(pool.id), "price": str(price), "volume_gb": volume_gb},
    )
    await message.answer(
        f"پلن آماده «{plan.name}» ساخته شد.\n"
        "حالا از منوی فروش کانفیگ آماده، فایل کانفیگ‌ها را برای همین پلن آپلود کنید."
    )


@router.callback_query(F.data == "ready_configs:upload")
async def choose_upload_pool(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    pools = await _load_pools(session)
    if not pools:
        await safe_edit_or_send(callback, "اول یک پلن آماده بسازید.")
        return
    builder = InlineKeyboardBuilder()
    for pool in pools:
        available, sold = await _pool_counts(session, pool.id)
        builder.button(
            text=f"{pool.plan.name} ({available} آماده / {sold} فروخته)",
            callback_data=ReadyPoolCallback(action="upload", pool_id=pool.id).pack(),
        )
    builder.button(text="بازگشت", callback_data="admin:ready_configs")
    builder.adjust(1)
    await safe_edit_or_send(callback, "پلنی که می‌خواهید فایل کانفیگ‌هایش را آپلود کنید انتخاب کنید:", reply_markup=builder.as_markup())


@router.callback_query(ReadyPoolCallback.filter(F.action == "upload"))
async def upload_pool_selected(
    callback: CallbackQuery,
    callback_data: ReadyPoolCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(pool_id=str(callback_data.pool_id))
    await state.set_state(ReadyConfigUploadStates.waiting_for_file)
    await safe_edit_or_send(
        callback,
        "فایل txt را ارسال کنید. هر خط یک کانفیگ جدا حساب می‌شود.\n"
        "شما می‌توانید لینک ساب سنایی را هم با علامت | کنار کانفیگ قرار دهید.\n"
        "مثال: vless://... | http://.../sub/12345\n"
        "اگر فایل ندارید، می‌توانید متن کانفیگ‌ها را هم همینجا پیست کنید.",
    )


@router.message(ReadyConfigUploadStates.waiting_for_file)
async def receive_ready_configs_file(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    admin_user: User,
) -> None:
    data = await state.get_data()
    pool_id = UUID(str(data["pool_id"]))
    source_name = "message.txt"
    raw_text = message.text or ""

    if message.document:
        source_name = message.document.file_name or "configs.txt"
        buffer = BytesIO()
        await message.bot.download(message.document, destination=buffer)
        raw_text = _decode_text_file(buffer.getvalue())

    lines = _parse_config_lines(raw_text)
    if not lines:
        await message.answer("هیچ کانفیگ معتبری داخل فایل/متن پیدا نشد.")
        return

    existing = set(
        await session.scalars(select(ReadyConfigItem.content).where(ReadyConfigItem.pool_id == pool_id))
    )
    created = 0
    for index, line in enumerate(lines, start=1):
        if line in existing:
            continue
        session.add(
            ReadyConfigItem(
                pool_id=pool_id,
                content=line,
                status="available",
                source_name=source_name,
                line_number=index,
            )
        )
        existing.add(line)
        created += 1

    await state.clear()
    await session.flush()
    await AuditLogRepository(session).log_action(
        actor_user_id=admin_user.id,
        action="upload_ready_configs",
        entity_type="ready_config_pool",
        entity_id=pool_id,
        payload={"source": source_name, "received": len(lines), "created": created},
    )
    await message.answer(f"{created} کانفیگ جدید به موجودی اضافه شد. {len(lines) - created} مورد تکراری بود.")


@router.callback_query(F.data == "ready_configs:list")
async def list_ready_configs(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    pools = await _load_pools(session)
    if not pools:
        await safe_edit_or_send(callback, "هنوز موجودی آماده‌ای تعریف نشده است.")
        return
    rows = []
    for pool in pools:
        available, sold = await _pool_counts(session, pool.id)
        status = "فعال" if pool.is_active and pool.plan.is_active else "غیرفعال"
        rows.append(
            f"{pool.plan.name}\n"
            f"وضعیت: {status}\n"
            f"حجم: {format_volume_bytes(pool.plan.volume_bytes)} | مدت: {pool.plan.duration_days} روز | قیمت: {pool.plan.price}\n"
            f"آماده: {available} | فروخته: {sold}"
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="بازگشت", callback_data="admin:ready_configs")
    await safe_edit_or_send(callback, "\n\n".join(rows), reply_markup=builder.as_markup())


async def _load_pools(session: AsyncSession) -> list[ReadyConfigPool]:
    result = await session.execute(
        select(ReadyConfigPool)
        .options(selectinload(ReadyConfigPool.plan))
        .join(ReadyConfigPool.plan)
        .order_by(ReadyConfigPool.created_at.desc())
    )
    return list(result.scalars().all())


async def _pool_counts(session: AsyncSession, pool_id: UUID) -> tuple[int, int]:
    available = int(
        await session.scalar(
            select(func.count()).select_from(ReadyConfigItem).where(
                ReadyConfigItem.pool_id == pool_id,
                ReadyConfigItem.status == "available",
            )
        )
        or 0
    )
    sold = int(
        await session.scalar(
            select(func.count()).select_from(ReadyConfigItem).where(
                ReadyConfigItem.pool_id == pool_id,
                ReadyConfigItem.status == "sold",
            )
        )
        or 0
    )
    return available, sold


def _parse_config_lines(raw_text: str) -> list[str]:
    return [line.strip() for line in raw_text.splitlines() if line.strip()]


def _decode_text_file(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _normalize_decimal_input(raw_value: str) -> str:
    normalized_characters: list[str] = []
    seen_decimal_separator = False
    for character in raw_value.strip():
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
        if character.isspace() or character in DECIMAL_SEPARATORS:
            continue
        try:
            normalized_characters.append(str(unicodedata.decimal(character)))
        except (TypeError, ValueError):
            normalized_characters.append(character)
    return "".join(normalized_characters)
