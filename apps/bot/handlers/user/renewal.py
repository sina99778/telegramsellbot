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


# Simple callback for payment method — data stored in FSM state
class RenewPayCallback(CallbackData, prefix="rp"):
    method: str  # 'wallet', 'nowpay', 'tetrapay', 'manual'


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

    # Store renewal data in FSM state
    await state.update_data(
        renew_amount=amount,
        renew_price=price,
    )
    await state.set_state(RenewStates.waiting_for_payment_method)

    # Show payment method selection
    gw = await settings_repo.get_gateway_settings()

    builder = InlineKeyboardBuilder()
    builder.button(
        text="👛 کیف پول",
        callback_data=RenewPayCallback(method="wallet").pack()
    )
    if gw.tetrapay_enabled:
        builder.button(
            text="💳 درگاه ریالی (تتراپی)",
            callback_data=RenewPayCallback(method="tetrapay").pack()
        )
    if gw.tronado_enabled:
        builder.button(
            text="درگاه ترونادو",
            callback_data=RenewPayCallback(method="tronado").pack()
        )
    if gw.nowpayments_enabled:
        builder.button(
            text="💎 درگاه ارزی (NOWPayments)",
            callback_data=RenewPayCallback(method="nowpay").pack()
        )
    if gw.manual_crypto_enabled and gw.manual_crypto_address:
        builder.button(
            text="💰 پرداخت به ولت (دستی)",
            callback_data=RenewPayCallback(method="manual").pack()
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


async def _get_renewal_data(state: FSMContext, session: AsyncSession, user_id: int):
    """Extract and validate renewal data from FSM state."""
    data = await state.get_data()
    sub_id_str = data.get("sub_id")
    renew_type = data.get("renew_type")
    renew_amount = data.get("renew_amount")
    renew_price = data.get("renew_price")

    if not all([sub_id_str, renew_type, renew_amount is not None, renew_price is not None]):
        return None

    user = await UserRepository(session).get_by_telegram_id(user_id)
    if user is None:
        return None

    sub_id = UUID(sub_id_str)
    return {
        "sub_id": sub_id,
        "renew_type": renew_type,
        "amount": float(renew_amount),
        "price": Decimal(str(renew_price)),
        "user": user,
    }


@router.callback_query(RenewPayCallback.filter(F.method == "wallet"))
async def renew_pay_wallet(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with wallet balance."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(state, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید یافت نشد. لطفاً دوباره تلاش کنید.")
        return

    user = rd["user"]
    price = rd["price"]
    sub_id = rd["sub_id"]
    renew_type = rd["renew_type"]
    amount = rd["amount"]

    if user.wallet is None:
        await safe_edit_or_send(callback, "کیف پول پیدا نشد.")
        return

    sub = await session.scalar(
        select(Subscription)
        .options(selectinload(Subscription.xui_client))
        .where(
            Subscription.id == sub_id,
            Subscription.user_id == user.id,
        )
    )
    if sub is None or sub.status not in ("active", "pending_activation", "expired"):
        await safe_edit_or_send(callback, "سرویس نامعتبر است.")
        return

    if sub.plan_id is None:
        await safe_edit_or_send(callback, "پلن این سرویس حذف شده. امکان تمدید وجود ندارد.")
        return

    if user.wallet.balance < price:
        await safe_edit_or_send(callback,
            f"موجودی کیف پول کافی نیست.\n"
            f"موجودی: {user.wallet.balance:.2f} USD\n"
            f"هزینه تمدید: {price:.2f} USD"
        )
        return

    await state.clear()
    await safe_edit_or_send(callback, "⏳ در حال تمدید...")

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
        metadata={"sub_id": str(sub.id), "type": renew_type},
    )

    # Apply renewal — if X-UI sync fails, the exception will propagate
    try:
        await _apply_renewal(sub, renew_type, amount, session)
    except Exception as exc:
        logger.error("Renewal X-UI sync failed for sub %s: %s", sub.id, exc, exc_info=True)
        # Refund the wallet debit since renewal was not applied on the panel
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=price,
            transaction_type="refund",
            direction="credit",
            currency="USD",
            reference_type="order",
            reference_id=order.id,
            description=f"Refund: renewal failed (panel unreachable)",
            metadata={"sub_id": str(sub.id), "error": str(exc)[:200]},
        )
        order.status = "failed"
        await safe_edit_or_send(callback,
            "❌ خطا در اعمال تمدید روی سرور!\n\n"
            "ارتباط با پنل X-UI برقرار نشد. مبلغ تمدید به کیف پول شما برگردانده شد.\n"
            "لطفاً بعداً دوباره تلاش کنید."
        )
        return

    await safe_edit_or_send(callback, Messages.RENEWAL_SUCCESS)

    # Notify admins
    await _notify_renewal_admins(callback, user, renew_type, amount, price, session)


@router.callback_query(RenewPayCallback.filter(F.method == "nowpay"))
async def renew_pay_nowpay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with NOWPayments gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(state, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید یافت نشد.")
        return

    user = rd["user"]
    price = rd["price"]
    await state.clear()

    from core.config import settings
    from models.payment import Payment
    from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
    from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError

    local_order_id = str(uuid4())

    renewal_meta = {
        "purpose": "renewal",
        "sub_id": str(rd["sub_id"]),
        "renew_type": rd["renew_type"],
        "renew_amount": rd["amount"],
    }

    payload = NowPaymentsPaymentCreateRequest(
        price_amount=price,
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Renewal for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )

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
        kind="direct_renewal",
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
        "بعد از پرداخت و تایید، تمدید به صورت خودکار اعمال می‌شود.",
        reply_markup=build_topup_link_keyboard(str(invoice.invoice_url)),
    )


@router.callback_query(RenewPayCallback.filter(F.method == "tetrapay"))
async def renew_pay_tetrapay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with TetraPay gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(state, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید یافت نشد.")
        return

    user = rd["user"]
    price = rd["price"]
    await state.clear()

    from core.config import settings
    from models.payment import Payment
    from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

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
        "sub_id": str(rd["sub_id"]),
        "renew_type": rd["renew_type"],
        "renew_amount": rd["amount"],
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
        kind="direct_renewal",
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
        "بعد از پرداخت و تایید، تمدید به صورت خودکار اعمال می‌شود.",
        reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_bot, bot_url=tx.payment_url_bot),
    )


@router.callback_query(RenewPayCallback.filter(F.method == "tronado"))
async def renew_pay_tronado(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return
    await callback.answer()

    data = await state.get_data()
    sub_id = UUID(data["sub_id"])
    renew_type = data["renew_type"]
    amount = float(data["renew_amount"])
    price = Decimal(str(data["renew_price"]))

    user = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if user is None:
        await safe_edit_or_send(callback, Messages.ACCOUNT_NOT_FOUND)
        return

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    from services.tronado.payments import create_tronado_invoice

    try:
        invoice = await create_tronado_invoice(
            session=session,
            user=user,
            amount_usd=price,
            kind="direct_renewal",
            description=f"Renewal sub {sub_id}",
            callback_payload={
                "sub_id": str(sub_id),
                "renew_type": renew_type,
                "renew_amount": amount,
                "purpose": "renewal",
                "source": "bot",
            },
        )
    except Exception as exc:
        await safe_edit_or_send(callback, f"خطا در ساخت فاکتور ترونادو: {exc}")
        return

    await state.clear()
    await safe_edit_or_send(
        callback,
        (
            "فاکتور تمدید ترونادو ساخته شد.\n\n"
            f"مبلغ: {price} USD\n"
            f"مقدار پرداخت: {invoice.tron_amount} TRX\n\n"
            "بعد از پرداخت و تایید، تمدید به صورت خودکار اعمال می‌شود."
        ),
        reply_markup=build_topup_link_keyboard(invoice.invoice_url),
    )


@router.callback_query(RenewPayCallback.filter(F.method == "manual"))
async def renew_pay_manual(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal via manual crypto — redirect to manual topup flow."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(state, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید یافت نشد.")
        return

    price = rd["price"]

    # Store the topup amount and redirect to manual crypto handler
    await state.update_data(topup_amount=str(price))

    from apps.bot.handlers.user.topup import topup_pay_manual
    await topup_pay_manual(callback, state, session)


# ─── Helpers ──────────────────────────────────────────────────────────────────


async def _apply_renewal(sub, renew_type: str, amount: float, session: AsyncSession) -> None:
    """Apply the actual renewal (volume or time) to the subscription and sync with X-UI.
    
    Delegates to services.renewal.apply_renewal which uses a savepoint to ensure
    that if X-UI sync fails, ALL DB changes are rolled back.
    """
    from services.renewal import apply_renewal
    await apply_renewal(
        session=session,
        subscription=sub,
        renew_type=renew_type,
        amount=amount,
    )


async def _notify_renewal_admins(callback, user, renew_type, amount, price, session) -> None:
    """Notify admins about a renewal."""
    from services.notifications import notify_admins
    user_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.telegram_id}'>مشاهده پروفایل</a>"
    renew_type_label = "حجم" if renew_type == "volume" else "زمان"
    admin_text = (
        "🔄 تمدید سرویس!\n\n"
        f"👤 کاربر: {user.first_name or '-'} | {user_link} (ID: <code>{user.telegram_id}</code>)\n"
        f"📦 نوع: {renew_type_label}\n"
        f"📊 مقدار: {amount}\n"
        f"💰 مبلغ: {price} USD"
    )
    try:
        bot = callback.bot
        await notify_admins(session, bot, admin_text)
    except Exception as exc:
        logger.warning("Failed to notify admins about renewal: %s", exc)
