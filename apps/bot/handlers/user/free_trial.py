from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.formatting import format_volume_bytes
from core.texts import Buttons, Messages
from models.order import Order
from models.plan import Plan
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager


router = Router(name="user-free-trial")


@router.message(F.text == Buttons.FREE_TRIAL)
async def free_trial_handler(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    user_repository = UserRepository(session)
    user = await user_repository.get_by_telegram_id(message.from_user.id)
    if user is None:
        await message.answer(Messages.ACCOUNT_NOT_FOUND)
        return

    if user.has_received_free_trial:
        await message.answer(Messages.TRIAL_ALREADY_RECEIVED)
        return

    trial_plan = await session.scalar(
        select(Plan).where(
            Plan.is_active.is_(True),
            or_(Plan.code == "TRIAL_PLAN", Plan.price == Decimal("0")),
        )
    )
    if trial_plan is None:
        await message.answer(Messages.TRIAL_PLAN_NOT_FOUND)
        return

    order = Order(
        user_id=user.id,
        plan_id=trial_plan.id,
        status="processing",
        source="bot",
        amount=Decimal("0"),
        currency=trial_plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        provisioning_manager = ProvisioningManager(session)
        provisioned = await provisioning_manager.provision_subscription(
            user_id=user.id,
            plan_id=trial_plan.id,
            order_id=order.id,
        )
    except ProvisioningError:
        order.status = "failed"
        await message.answer(Messages.PROVISIONING_FAILED_REFUNDED)
        return

    await user_repository.mark_free_trial_received(user.id)
    order.status = "provisioned"

    subscription = provisioned.subscription
    xui_record = provisioned.xui_client
    sub_link = subscription.sub_link or xui_record.sub_link or "-"

    await message.answer(
        Messages.CONFIG_CREATED.format(
            plan_name=trial_plan.name,
            volume_label=format_volume_bytes(trial_plan.volume_bytes),
            client_email=xui_record.email,
            sub_link=sub_link,
        )
    )
