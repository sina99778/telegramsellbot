from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID, uuid4

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


class RenewPayMethodCallback(CallbackData, prefix="renew_pay"):
    sub_id: UUID
    type: str
    amount: float
    price: float
    method: str  # 'wallet', 'nowpay', 'tetrapay'


from apps.bot.utils.messaging import safe_edit_or_send

@router.callback_query(MyConfigCallback.filter(F.action == "renew"))
async def renew_config_start(callback: CallbackQuery, callback_data: MyConfigCallback, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    
    markup = build_renewal_keyboard(callback_data.subscription_id)
    await safe_edit_or_send(callback, Messages.RENEWAL_OPTIONS, reply_markup=markup)


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

    # Show payment method selection
    gw = await settings_repo.get_gateway_settings()

    builder = InlineKeyboardBuilder()
    builder.button(
        text="👛 کیف پول",
        callback_data=RenewPayMethodCallback(
            sub_id=sub_id, type=renew_type, amount=amount, price=price, method="wallet"
        ).pack()
    )
    if gw.tetrapay_enabled:
        builder.button(
            text="💳 درگاه ریالی (تتراپی)",
            callback_data=RenewPayMethodCallback(
                sub_id=sub_id, type=renew_type, amount=amount, price=price, method="tetrapay"
            ).pack()
        )
    if gw.nowpayments_enabled:
        builder.button(
            text="💎 درگاه ارزی (NOWPayments)",
            callback_data=RenewPayMethodCallback(
                sub_id=sub_id, type=renew_type, amount=amount, price=price, method="nowpay"
            ).pack()
        )
    if gw.manual_crypto_enabled and gw.manual_crypto_address:
        builder.button(
            text="💰 پرداخت به ولت (دستی)",
            callback_data=RenewPayMethodCallback(
                sub_id=sub_id, type=renew_type, amount=amount, price=price, method="manual"
            ).pack()
        )
    builder.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack())
    builder.adjust(1)
    
    text = Messages.RENEWAL_INVOICE.format(
        volume=volume_added,
        time=time_added_days,
        price=price
    )
    text += "\n\n💳 روش پرداخت را انتخاب کنید:"
    
    await message.answer(text, reply_markup=builder.as_markup())
    await state.clear()


@router.callback_query(RenewPayMethodCallback.filter(F.method == "wallet"))
async def renew_pay_wallet(
    callback: CallbackQuery,
    callback_data: RenewPayMethodCallback,
    session: AsyncSession,
) -> None:
    """Pay renewal with wallet balance."""
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
    if sub is None or sub.status not in ("active", "pending_activation", "expired"):
        await callback.message.edit_text("سرویس نامعتبر است.")
        return

    if sub.plan_id is None:
        await callback.message.edit_text("پلن این سرویس حذف شده. امکان تمدید وجود ندارد.")
        return

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

    # Link order to subscription
    sub.order_id = order.id

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

    # Apply renewal
    await _apply_renewal(sub, callback_data.type, callback_data.amount, session)

    await callback.message.edit_text(Messages.RENEWAL_SUCCESS)

    # Notify admins
    await _notify_renewal_admins(callback, user, callback_data, price, session)


@router.callback_query(RenewPayMethodCallback.filter(F.method == "nowpay"))
async def renew_pay_nowpay(
    callback: CallbackQuery,
    callback_data: RenewPayMethodCallback,
    session: AsyncSession,
) -> None:
    """Pay renewal with NOWPayments gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    from core.config import settings
    from models.payment import Payment
    from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
    from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "حساب پیدا نشد.")
        return

    price = Decimal(str(callback_data.price))
    local_order_id = str(uuid4())

    renewal_meta = {
        "purpose": "renewal",
        "sub_id": str(callback_data.sub_id),
        "renew_type": callback_data.type,
        "renew_amount": callback_data.amount,
    }

    payload = NowPaymentsPaymentCreateRequest(
        price_amount=price,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Renewal for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )

    # Use DB-configured API key if available
    gw = await AppSettingsRepository(session).get_gateway_settings()
    from pydantic import SecretStr
    effective_api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key

    try:
        async with NowPaymentsClient(
            NowPaymentsClientConfig(
                api_key=effective_api_key,
                base_url=settings.nowpayments_base_url,
            )
        ) as client:
            invoice = await client.create_payment_invoice(payload)
    except NowPaymentsRequestError:
        await safe_edit_or_send(callback, Messages.PAYMENT_GATEWAY_UNAVAILABLE)
        return

    payment = Payment(
        user_id=user.id,
        provider="nowpayments",
        kind="wallet_topup",
        provider_invoice_id=str(invoice.id),
        order_id=local_order_id,
        payment_status="waiting",
        price_currency="USD",
        price_amount=price,
        invoice_url=str(invoice.invoice_url),
        callback_payload=renewal_meta,
    )
    session.add(payment)
    await session.flush()

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    await safe_edit_or_send(
        callback,
        f"🧾 فاکتور تمدید ساخته شد:\n\n"
        f"💰 مبلغ: {price} USD\n\n"
        "بعد از پرداخت، مبلغ به کیف پول واریز می‌شود.\n"
        "سپس مجدداً از طریق کیف پول تمدید کنید.",
        reply_markup=build_topup_link_keyboard(str(invoice.invoice_url)),
    )


@router.callback_query(RenewPayMethodCallback.filter(F.method == "tetrapay"))
async def renew_pay_tetrapay(
    callback: CallbackQuery,
    callback_data: RenewPayMethodCallback,
    session: AsyncSession,
) -> None:
    """Pay renewal with TetraPay gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    from core.config import settings
    from models.payment import Payment
    from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, "حساب پیدا نشد.")
        return

    price = Decimal(str(callback_data.price))

    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        await safe_edit_or_send(callback, "❌ نرخ تبدیل تومان تنظیم نشده.")
        return

    toman_amount = int((price * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10

    if rial_amount < 10000:
        await safe_edit_or_send(callback, "❌ مبلغ کمتر از حداقل مجاز درگاه است.")
        return

    local_order_id = str(uuid4())

    renewal_meta = {
        "purpose": "renewal",
        "sub_id": str(callback_data.sub_id),
        "renew_type": callback_data.type,
        "renew_amount": callback_data.amount,
    }

    gw = await AppSettingsRepository(session).get_gateway_settings()
    effective_key = gw.tetrapay_api_key if gw.tetrapay_api_key else settings.tetrapay_api_key.get_secret_value()

    try:
        async with TetraPayClient(
            TetraPayClientConfig(
                api_key=effective_key,
                base_url=settings.tetrapay_base_url,
            )
        ) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"تمدید سرویس - کاربر {user.telegram_id}",
                email=f"{user.telegram_id}@telegram.org",
                mobile="09111111111",
            )
    except TetraPayRequestError as exc:
        await safe_edit_or_send(callback, f"❌ خطا در ساخت فاکتور: {exc}")
        return

    payment = Payment(
        user_id=user.id,
        provider="tetrapay",
        kind="wallet_topup",
        provider_payment_id=tx.Authority,
        order_id=local_order_id,
        payment_status="waiting",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=price,
        pay_amount=toman_amount,
        invoice_url=tx.payment_url_bot,
        callback_payload=renewal_meta,
    )
    session.add(payment)
    await session.flush()

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    await safe_edit_or_send(
        callback,
        f"🔖 فاکتور تمدید (ریالی):\n\n"
        f"💵 مبلغ: {toman_amount:,} تومان\n\n"
        "بعد از پرداخت، مبلغ به کیف پول واریز می‌شود.\n"
        "سپس مجدداً از طریق کیف پول تمدید کنید.",
        reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_bot, bot_url=tx.payment_url_bot),
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _apply_renewal(sub, renew_type: str, amount: float, session: AsyncSession) -> None:
    """Apply the actual renewal (volume or time) to the subscription and sync with X-UI."""
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)

    if renew_type == "volume":
        bytes_to_add = int(amount * 1024**3)
        sub.volume_bytes += bytes_to_add

    if renew_type == "time":
        days_to_add = int(amount)
        if sub.ends_at is None:
            if sub.activated_at is not None:
                sub.ends_at = sub.activated_at + timedelta(days=days_to_add)
            elif sub.status == "expired":
                sub.ends_at = now_utc + timedelta(days=days_to_add)
        else:
            if sub.ends_at < now_utc:
                sub.ends_at = now_utc + timedelta(days=days_to_add)
            else:
                sub.ends_at += timedelta(days=days_to_add)

    # Change status back to active if it was expired
    if sub.status == "expired":
        sub.status = "active"

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

                    # Extract subId from sub_link
                    existing_sub_id = ""
                    current_sub_link = sub.sub_link or (xui_full.sub_link if xui_full else "") or ""
                    if current_sub_link and "/" in current_sub_link:
                        existing_sub_id = current_sub_link.rsplit("/", 1)[-1]

                    # Reset X-UI state and make active
                    xui_c = XUIClient(
                        id=xui_full.client_uuid,
                        uuid=xui_full.client_uuid,
                        email=xui_full.email,
                        enable=True,
                        limitIp=1,
                        totalGB=sub.volume_bytes,
                        expiryTime=expiry_time,
                        subId=existing_sub_id,
                        comment=xui_full.username or "",
                    )
                    
                    # Ensure XUI record is also marked as active locally
                    xui_full.is_active = True
                    
                    await client.update_client(
                        inbound_id=xui_full.inbound.xui_inbound_remote_id,
                        client_id=xui_full.client_uuid,
                        client=xui_c,
                    )
            except Exception as e:
                logger.error("Failed to sync X-UI limit on renewal: %s", e, exc_info=True)

    await session.flush()


async def _notify_renewal_admins(callback, user, callback_data, price, session) -> None:
    """Notify admins about a renewal."""
    from services.notifications import notify_admins
    user_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.telegram_id}'>مشاهده پروفایل</a>"
    renew_type_label = "حجم" if callback_data.type == "volume" else "زمان"
    admin_text = (
        "🔄 تمدید سرویس!\n\n"
        f"👤 کاربر: {user.first_name or '-'} | {user_link} (ID: <code>{user.telegram_id}</code>)\n"
        f"📦 نوع: {renew_type_label}\n"
        f"📊 مقدار: {callback_data.amount}\n"
        f"💰 مبلغ: {price} USD"
    )
    try:
        bot = callback.bot
        await notify_admins(session, bot, admin_text)
    except Exception as exc:
        logger.warning("Failed to notify admins about renewal: %s", exc)


@router.callback_query(RenewPayMethodCallback.filter(F.method == "manual"))
async def renew_pay_manual(
    callback: CallbackQuery,
    callback_data: RenewPayMethodCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal via manual crypto — redirect to manual topup flow."""
    if callback.from_user is None:
        return
    await callback.answer()

    from decimal import Decimal
    price = Decimal(str(callback_data.price))

    # Store the topup amount and redirect to manual crypto handler
    await state.update_data(topup_amount=str(price))

    from apps.bot.handlers.user.topup import topup_pay_manual
    await topup_pay_manual(callback, state, session)
