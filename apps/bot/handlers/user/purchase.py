from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.inline import build_plan_selection_keyboard, build_wallet_topup_keyboard
from core.config import settings
from models.order import Order
from models.plan import Plan
from repositories.user import UserRepository
from services.provisioning.manager import ProvisioningError, ProvisioningManager
from services.wallet.manager import InsufficientBalanceError, WalletManager
from services.xui.client import SanaeiXUIClient, XUIClientConfig


router = Router(name="user-purchase")


@router.callback_query(F.data == "pagination:noop")
async def ignore_pagination_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text == "🛍 Buy Config")
async def show_available_plans(message: Message, session: AsyncSession) -> None:
    result = await session.execute(
        select(Plan)
        .where(Plan.is_active.is_(True))
        .order_by(Plan.price.asc(), Plan.duration_days.asc())
    )
    plans = list(result.scalars().all())
    if not plans:
        await message.answer("No plans are available right now. Please try again later.")
        return

    await message.answer(
        "Choose a plan to purchase:",
        reply_markup=build_plan_selection_keyboard(plans),
    )


@router.callback_query(F.data.startswith("plan:select:"))
async def purchase_plan_callback(
    callback: CallbackQuery,
    session: AsyncSession,
) -> None:
    await callback.answer()
    if callback.from_user is None:
        return

    raw_plan_id = callback.data.rsplit(":", 1)[-1]
    try:
        plan_id = UUID(raw_plan_id)
    except ValueError:
        await callback.message.answer("The selected plan is invalid.")
        return

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    plan = await session.get(Plan, plan_id)
    if user is None or user.wallet is None or plan is None or not plan.is_active:
        await callback.message.answer("This plan is not available right now.")
        return

    if user.wallet.balance < plan.price:
        await callback.message.answer(
            (
                f"Your wallet balance is {user.wallet.balance} USD, but this plan costs {plan.price} {plan.currency}.\n"
                "Please top up your wallet first."
            ),
            reply_markup=build_wallet_topup_keyboard(),
        )
        return

    wallet_manager = WalletManager(session)
    order = Order(
        user_id=user.id,
        plan_id=plan.id,
        status="processing",
        source="bot",
        amount=plan.price,
        currency=plan.currency,
    )
    session.add(order)
    await session.flush()

    try:
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(plan.price)),
            transaction_type="purchase",
            direction="debit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description=f"Purchase of plan {plan.code}",
            metadata={"plan_id": str(plan.id)},
        )
    except InsufficientBalanceError:
        order.status = "failed"
        await callback.message.answer("Your balance is no longer sufficient. Please top up and try again.")
        return

    try:
        async with SanaeiXUIClient(
            XUIClientConfig(
                base_url=settings.xui_base_url,
                username=settings.xui_username,
                password=settings.xui_password,
            )
        ) as xui_client:
            provisioning_manager = ProvisioningManager(session, xui_client)
            provisioned = await provisioning_manager.provision_subscription(
                user_id=user.id,
                plan_id=plan.id,
                order_id=order.id,
            )
    except ProvisioningError:
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(plan.price)),
            transaction_type="refund",
            direction="credit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description="Automatic refund after provisioning failure",
            metadata={"plan_id": str(plan.id)},
        )
        order.status = "refunded"
        await callback.message.answer(
            "The panel could not create your configuration right now. Your wallet has been refunded automatically."
        )
        return

    order.status = "provisioned"
    subscription = provisioned.subscription
    xui_record = provisioned.xui_client
    sub_link = subscription.sub_link or xui_record.sub_link or "Not available yet"

    await callback.message.answer(
        (
            "Your configuration has been created successfully.\n\n"
            f"Plan: {plan.name}\n"
            f"Volume: {plan.volume_bytes} bytes\n"
            f"Client: {xui_record.email}\n"
            f"Sub Link: {sub_link}\n\n"
            "Activation starts on first use."
        )
    )
