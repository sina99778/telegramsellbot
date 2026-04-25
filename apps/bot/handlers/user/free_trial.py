from __future__ import annotations

from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.texts import Buttons
from models.order import Order
from models.plan import Plan
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager

router = Router(name="user-free-trial")


@router.message(F.text == Buttons.TEST_CONFIG)
async def free_trial_handler(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer("حساب شما پیدا نشد. لطفا /start را بزنید.")
        return

    trial_settings = await AppSettingsRepository(session).get_trial_settings()
    if not trial_settings.enabled:
        await message.answer("کانفیگ تست فعلا غیرفعال است.")
        return

    if user.has_received_free_trial:
        await message.answer("شما قبلا کانفیگ تست دریافت کرده‌اید.")
        return

    plan = await session.scalar(
        select(Plan)
        .where(Plan.is_active.is_(True))
        .order_by(Plan.price.asc(), Plan.duration_days.asc())
        .limit(1)
    )
    if plan is None:
        await message.answer("فعلا پلن فعالی برای ساخت کانفیگ تست وجود ندارد.")
        return

    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status="processing",
        source="trial",
        amount=Decimal("0"),
        currency=plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        result = await ProvisioningManager(session).provision_subscription(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
            config_name=f"trial_{user.telegram_id}",
        )
    except ProvisioningError as exc:
        order.status = "failed"
        await message.answer(f"ساخت کانفیگ تست ناموفق بود:\n{exc}")
        return

    user.has_received_free_trial = True
    order.status = "provisioned"
    await message.answer(
        "کانفیگ تست شما آماده است:\n\n"
        f"Sub link:\n{result.sub_link}\n\n"
        f"Config:\n{result.vless_uri}"
    )
