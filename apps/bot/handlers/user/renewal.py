from __future__ import annotations

import logging
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.bot.handlers.user.my_configs import MyConfigCallback
from apps.bot.keyboards.inline import build_renewal_keyboard
from apps.bot.states.renew import RenewStates
from core.texts import Buttons, Messages
from models.order import Order
from models.subscription import Subscription
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.xui.client import SanaeiXUIClient, XUIClient, XUIRequestError
from services.xui.runtime import build_xui_client_config, ensure_inbound_server_loaded

logger = logging.getLogger(__name__)

router = Router(name="user-renewal")


class RenewTypeCallback(CallbackData, prefix="renew"):
    type: str # 'volume' or 'time'
    sub_id: UUID


class RenewConfirmCallback(CallbackData, prefix="renew_confirm"):
    sub_id: UUID
    type: str
    amount: float
    price: float


@router.callback_query(MyConfigCallback.filter(F.action == "renew"))
async def renew_config_start(callback: CallbackQuery, callback_data: MyConfigCallback, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    
    markup = build_renewal_keyboard(callback_data.subscription_id)
    await callback.message.edit_text(Messages.RENEWAL_OPTIONS, reply_markup=markup)


@router.callback_query(RenewTypeCallback.filter())
async def renew_type_selected(callback: CallbackQuery, callback_data: RenewTypeCallback, state: FSMContext) -> None:
    await callback.answer()
    
    await state.update_data(sub_id=str(callback_data.sub_id), renew_type=callback_data.type)
    
    builder = InlineKeyboardBuilder()
    builder.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=callback_data.sub_id).pack())
    builder.adjust(1)
    
    if callback_data.type == "volume":
        await state.set_state(RenewStates.waiting_for_volume)
        await callback.message.edit_text(Messages.RENEWAL_ENTER_VOLUME, reply_markup=builder.as_markup())
    else:
        await state.set_state(RenewStates.waiting_for_time)
        await callback.message.edit_text(Messages.RENEWAL_ENTER_TIME, reply_markup=builder.as_markup())


@router.message(RenewStates.waiting_for_volume)
@router.message(RenewStates.waiting_for_time)
async def renew_value_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    data = await state.get_data()
    sub_id = UUID(data["sub_id"])
    renew_type = data["renew_type"]

    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(Messages.RENEWAL_INVALID_VALUE)
        return

    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()

    volume_added = 0.0
    time_added_days = 0.0
    
    if renew_type == "volume":
        price = amount * renewal_settings.price_per_gb
        volume_added = amount
    elif renew_type == "time":
        price = (amount / 10.0) * renewal_settings.price_per_10_days
        time_added_days = amount
        
    price = round(price, 2)
    
    confirm_markup = InlineKeyboardBuilder()
    confirm_markup.button(
        text="✅ تایید و پرداخت",
        callback_data=RenewConfirmCallback(
            sub_id=sub_id,
            type=renew_type,
            amount=amount,
            price=price
        ).pack()
    )
    confirm_markup.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack())
    confirm_markup.adjust(1)
    
    text = Messages.RENEWAL_INVOICE.format(
        volume=volume_added,
        time=time_added_days,
        price=price
    )
    
    await message.answer(text, reply_markup=confirm_markup.as_markup())
    await state.clear()


@router.callback_query(RenewConfirmCallback.filter())
async def renew_confirm_payment(
    callback: CallbackQuery,
    callback_data: RenewConfirmCallback,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return

    await callback.answer()

    user_repo = UserRepository(session)
    user = await user_repo.get_by_telegram_id(callback.from_user.id)
    if user is None or user.wallet is None:
        await callback.message.edit_text("حساب یا کیف پول پیدا نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.xui_client))
        .where(
            Subscription.id == callback_data.sub_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None or sub.status not in ("active", "pending_activation"):
        await callback.message.edit_text("سرویس نامعتبر است.")
        return

    from decimal import Decimal
    price = Decimal(str(callback_data.price))

    if user.wallet.balance < price:
        await callback.message.edit_text(
            f"موجودی کیف پول کافی نیست.\n"
            f"موجودی: {user.wallet.balance:.2f} USD\n"
            f"هزینه تمدید: {price:.2f} USD"
        )
        return

    await callback.message.edit_text("⏳ در حال تمدید...")

    # Create order
    order = Order(
        user_id=user.id,
        plan_id=sub.plan_id,
        amount=price,
        currency="USD",
        status="completed",
        source="bot",
    )
    session.add(order)
    await session.flush()

    # Deduct from wallet using WalletManager
    from services.wallet.manager import WalletManager
    wallet_manager = WalletManager(session)
    await wallet_manager.process_transaction(
        user_id=user.id,
        amount=price,
        transaction_type="renewal",
        direction="debit",
        currency="USD",
        reference_type="order",
        reference_id=order.id,
        description=f"Renewal of subscription {sub.id}",
        metadata={"sub_id": str(sub.id), "type": callback_data.type},
    )

    # Calculate actual bytes or timeframe
    from datetime import datetime, timezone, timedelta

    if callback_data.type == "volume":
        bytes_to_add = int(callback_data.amount * 1024**3)
        sub.volume_bytes += bytes_to_add

    if callback_data.type == "time":
        days_to_add = int(callback_data.amount)
        if sub.ends_at is None:
            if sub.activated_at is not None:
                sub.ends_at = sub.activated_at + timedelta(days=days_to_add)
        else:
            sub.ends_at += timedelta(days=days_to_add)

    # Sync with X-UI panel
    xui = sub.xui_client
    if xui:
        from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord

        xui_full = await session.scalar(
            select(XUIClientRecord)
            .options(
                selectinload(XUIClientRecord.inbound)
                .selectinload(XUIInboundRecord.server)
                .selectinload(XUIServerRecord.credentials)
            )
            .where(XUIClientRecord.id == xui.id)
        )

        if xui_full and xui_full.inbound and xui_full.inbound.server:
            try:
                server = ensure_inbound_server_loaded(xui_full.inbound)
                config = build_xui_client_config(server)
                async with SanaeiXUIClient(config) as client:
                    expiry_time = 0
                    if sub.ends_at:
                        expiry_time = int(sub.ends_at.timestamp() * 1000)

                    xui_c = XUIClient(
                        id=xui_full.client_uuid,
                        uuid=xui_full.client_uuid,
                        email=xui_full.email,
                        enable=True,
                        totalGB=sub.volume_bytes,
                        expiryTime=expiry_time,
                    )
                    await client.update_client(
                        inbound_id=xui_full.inbound.xui_inbound_remote_id,
                        client_id=xui_full.client_uuid,
                        client=xui_c,
                    )
            except Exception as e:
                logger.error("Failed to sync X-UI limit on renewal: %s", e, exc_info=True)

    await session.flush()
    await callback.message.edit_text(Messages.RENEWAL_SUCCESS)
