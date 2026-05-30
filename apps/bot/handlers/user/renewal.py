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
from core.redis import distributed_lock
from core.texts import Buttons, Messages
from models.order import Order
from models.plan import Plan
from models.subscription import Subscription
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.xui.client import SanaeiXUIClient, XUIClient, XUIRequestError
from services.xui.runtime import build_xui_client_config, ensure_inbound_server_loaded

logger = logging.getLogger(__name__)

router = Router(name="user-renewal")

_MIN_VOLUME_GB = 0.1
_MIN_TIME_DAYS = 1


class RenewTypeCallback(CallbackData, prefix="renew"):
    type: str # 'volume' or 'time'
    sub_id: UUID


class RenewPresetCallback(CallbackData, prefix="renewpre"):
    type: str  # 'volume' or 'time'
    sub_id: UUID
    value: int  # GB for volume, days for time

# Default presets — chosen to cover the common shape of buyer behaviour.
_VOLUME_PRESETS_GB = (10, 30, 50, 100)
_TIME_PRESETS_DAYS = (30, 60, 90)


def _build_renew_preset_keyboard(sub_id: UUID, renew_type: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    presets = _VOLUME_PRESETS_GB if renew_type == "volume" else _TIME_PRESETS_DAYS
    unit = "GB" if renew_type == "volume" else "روز"
    for v in presets:
        builder.button(
            text=f"➕ {v} {unit}",
            callback_data=RenewPresetCallback(type=renew_type, sub_id=sub_id, value=v).pack(),
        )
    builder.button(text="✏️ مقدار دلخواه", callback_data=f"renew:custom:{renew_type}:{sub_id.hex}")
    builder.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack())
    builder.adjust(2, 2, 1, 1)
    return builder


# Simple callback for payment method — data stored in callback
class RenewPayCallback(CallbackData, prefix="rp"):
    m: str  # method: 'w', 'n', 't', 'm', 'tr'
    s: str  # sub_id hex
    t: str  # type: 'v' or 't'
    a: str  # amount string


# Same shape, separate prefix → partial-payment routes that only invoice
# the gap between wallet balance and full renewal cost.
class RenewPartialPayCallback(CallbackData, prefix="rpp"):
    m: str
    s: str
    t: str
    a: str


from apps.bot.utils.messaging import safe_edit_or_send

@router.callback_query(MyConfigCallback.filter(F.action == "renew"))
async def renew_config_start(callback: CallbackQuery, callback_data: MyConfigCallback, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()

    # Check if renewals are enabled by admin (admins bypass)
    user_actions = await AppSettingsRepository(session).get_user_actions_settings()
    if not user_actions.renewals_enabled:
        from core.config import settings as app_settings
        from repositories.user import UserRepository
        user_record = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
        is_admin = (
            (user_record and user_record.role in {"admin", "owner"})
            or callback.from_user.id == app_settings.owner_telegram_id
        )
        if not is_admin:
            await safe_edit_or_send(callback, "⛔ تمدید سرویس توسط مدیر موقتاً غیرفعال شده است. لطفاً بعداً تلاش کنید.")
            return

    # Ownership + status gate: load the sub here so suspended/revoked configs
    # can't be quietly re-activated via renewal flow, and IDs from a forged
    # callback can't reach a subscription owned by a different user.
    from repositories.user import UserRepository
    from sqlalchemy import select as _sel
    requester = await UserRepository(session).get_by_telegram_id(callback.from_user.id)
    if requester is None:
        await safe_edit_or_send(callback, "حساب شما پیدا نشد.")
        return
    sub = await session.scalar(
        _sel(Subscription).where(
            Subscription.id == callback_data.subscription_id,
            Subscription.user_id == requester.id,
        )
    )
    if sub is None:
        await safe_edit_or_send(callback, "سرویس نامعتبر است.")
        return
    if sub.status not in ("active", "pending_activation", "expired"):
        await safe_edit_or_send(
            callback,
            "⛔ این سرویس قابل تمدید نیست. لطفاً برای رفع مشکل با پشتیبانی تماس بگیرید.",
        )
        return

    await state.clear()

    markup = build_renewal_keyboard(callback_data.subscription_id)
    await safe_edit_or_send(callback, Messages.RENEWAL_OPTIONS, reply_markup=markup)


@router.callback_query(RenewTypeCallback.filter())
async def renew_type_selected(callback: CallbackQuery, callback_data: RenewTypeCallback, state: FSMContext) -> None:
    await callback.answer()

    await state.update_data(sub_id=str(callback_data.sub_id), renew_type=callback_data.type)
    # Don't lock the FSM into "waiting_for_X" yet — give the user preset
    # buttons first. Only when they tap "custom" do we go into waiting state.

    kb = _build_renew_preset_keyboard(callback_data.sub_id, callback_data.type)
    if callback_data.type == "volume":
        text = (
            "🔋 <b>تمدید حجم</b>\n\n"
            "حجم اضافی را انتخاب کنید یا «مقدار دلخواه» را بزنید."
        )
    else:
        text = (
            "📅 <b>تمدید زمان</b>\n\n"
            "مدت اضافی را انتخاب کنید یا «مقدار دلخواه» را بزنید."
        )
    await callback.message.edit_text(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("renew:custom:"))
async def renew_custom_amount(callback: CallbackQuery, state: FSMContext) -> None:
    """User picked 'custom amount' on the preset keyboard."""
    await callback.answer()
    parts = callback.data.split(":")
    # parts = ["renew", "custom", "<volume|time>", "<sub_hex>"]
    if len(parts) != 4:
        return
    renew_type = parts[2]
    sub_id = UUID(parts[3])
    await state.update_data(sub_id=str(sub_id), renew_type=renew_type)
    builder = InlineKeyboardBuilder()
    builder.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack())
    builder.adjust(1)
    if renew_type == "volume":
        await state.set_state(RenewStates.waiting_for_volume)
        await callback.message.edit_text(Messages.RENEWAL_ENTER_VOLUME, reply_markup=builder.as_markup())
    else:
        await state.set_state(RenewStates.waiting_for_time)
        await callback.message.edit_text(Messages.RENEWAL_ENTER_TIME, reply_markup=builder.as_markup())


@router.callback_query(RenewPresetCallback.filter())
async def renew_preset_selected(
    callback: CallbackQuery,
    callback_data: RenewPresetCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """User picked a preset value — feed it directly to the existing flow."""
    await callback.answer()
    await state.update_data(
        sub_id=str(callback_data.sub_id),
        renew_type=callback_data.type,
    )
    # Match the state the message-based path expects, then synthesize a
    # message-like object that carries just the .text attribute.
    if callback_data.type == "volume":
        await state.set_state(RenewStates.waiting_for_volume)
    else:
        await state.set_state(RenewStates.waiting_for_time)

    # Reuse the existing message handler by faking a minimal Message-like
    # adapter. It uses .text and .answer / .from_user — provide them.
    class _Pseudo:
        text = str(callback_data.value)
        from_user = callback.from_user

        async def answer(self, *a, **kw):
            if callback.message:
                return await callback.message.answer(*a, **kw)

    await renew_value_entered(_Pseudo(), state, session)


@router.message(RenewStates.waiting_for_volume)
@router.message(RenewStates.waiting_for_time)
async def renew_value_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text:
        return

    data = await state.get_data()
    sub_id = UUID(data["sub_id"])
    renew_type = data["renew_type"]

    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(Messages.RENEWAL_INVALID_VALUE)
        return

    # Minimum amount validation
    if renew_type == "volume" and amount < _MIN_VOLUME_GB:
        await message.answer(f"❌ حداقل حجم قابل افزودن {_MIN_VOLUME_GB} گیگابایت است.")
        return
    if renew_type == "time" and amount < _MIN_TIME_DAYS:
        await message.answer("❌ حداقل مدت قابل افزودن ۱ روز است.")
        return

    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()

    # Look up the sub's plan so we can prefer its per-plan overrides
    # (renewal_price_per_gb / renewal_price_per_day) over the global
    # defaults. Falls back gracefully if the plan was deleted.
    plan = await session.scalar(
        select(Plan).join(Subscription, Subscription.plan_id == Plan.id).where(Subscription.id == sub_id)
    )

    volume_added = 0.0
    time_added_days = 0.0

    if renew_type == "volume":
        per_gb = plan.effective_renewal_price_per_gb(renewal_settings.price_per_gb) if plan else renewal_settings.price_per_gb
        price = amount * per_gb
        volume_added = amount
    elif renew_type == "time":
        per_day = plan.effective_renewal_price_per_day(renewal_settings.price_per_10_days) if plan else (renewal_settings.price_per_10_days / 10.0)
        price = amount * per_day
        time_added_days = amount

    # Apply personal discount so the displayed price matches the actual charge
    if message.from_user:
        user = await UserRepository(session).get_by_telegram_id(message.from_user.id)
        discount_pct = getattr(user, "personal_discount_percent", 0) or 0 if user else 0
        if discount_pct > 0:
            price = price * (1.0 - (discount_pct / 100.0))

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
        callback_data=RenewPayCallback(m="w", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
    )
    if gw.tetrapay_enabled:
        builder.button(
            text="💳 درگاه ریالی (تتراپی)",
            callback_data=RenewPayCallback(m="t", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
        )
    if gw.card_to_card_enabled and (gw.cards or gw.card_number):
        builder.button(
            text="💵 کارت به کارت",
            callback_data=RenewPayCallback(m="c", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
        )
    if gw.tronado_enabled:
        builder.button(
            text="🪙 درگاه ترونادو",
            callback_data=RenewPayCallback(m="tr", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
        )
    if gw.nowpayments_enabled:
        builder.button(
            text="💎 درگاه ارزی (NOWPayments)",
            callback_data=RenewPayCallback(m="n", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
        )
    if gw.manual_crypto_enabled and gw.manual_crypto_address:
        builder.button(
            text="💰 پرداخت به ولت (دستی)",
            callback_data=RenewPayCallback(m="m", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack()
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


async def _get_renewal_data(callback_data: RenewPayCallback, session: AsyncSession, user_id: int):
    """Extract and validate renewal data from callback data directly."""
    sub_id = UUID(callback_data.s)
    renew_type = "volume" if callback_data.t == "v" else "time"
    try:
        amount = float(callback_data.a)
        if amount <= 0:
            return None
    except ValueError:
        return None

    user = await UserRepository(session).get_by_telegram_id(user_id)
    if user is None:
        return None

    settings_repo = AppSettingsRepository(session)
    renewal_settings = await settings_repo.get_renewal_settings()

    # Mirror renew_value_entered's per-plan-pricing logic, so the price
    # the user is actually charged on this callback matches the one we
    # showed them in the invoice.
    plan = await session.scalar(
        select(Plan).join(Subscription, Subscription.plan_id == Plan.id).where(Subscription.id == sub_id)
    )
    if renew_type == "volume":
        per_gb = plan.effective_renewal_price_per_gb(renewal_settings.price_per_gb) if plan else renewal_settings.price_per_gb
        price = amount * per_gb
    else:
        per_day = plan.effective_renewal_price_per_day(renewal_settings.price_per_10_days) if plan else (renewal_settings.price_per_10_days / 10.0)
        price = amount * per_day

    # Apply personal discount
    discount_pct = getattr(user, "personal_discount_percent", 0) or 0
    if discount_pct > 0:
        price = price * (1.0 - (discount_pct / 100.0))

    price = round(Decimal(str(price)), 2)

    return {
        "sub_id": sub_id,
        "renew_type": renew_type,
        "amount": amount,
        "price": price,
        "user": user,
    }


@router.callback_query(RenewPayCallback.filter(F.m == "w"))
async def renew_pay_wallet(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with wallet balance."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
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
        # Partial-payment offer: instead of blocking, show buttons to
        # pay the gap (price - wallet_balance) via gateway. When the
        # gateway IPN settles, _handle_direct_renewal debits the FULL
        # `price` against the wallet, draining what's there + the
        # just-credited gateway portion.
        gap = (Decimal(str(price)) - user.wallet.balance).quantize(Decimal("0.01"))
        gw = await AppSettingsRepository(session).get_gateway_settings()
        builder = InlineKeyboardBuilder()
        if gw.tetrapay_enabled:
            builder.button(
                text=f"💳 پرداخت {gap} $ از درگاه ریالی",
                callback_data=RenewPartialPayCallback(m="t", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack(),
            )
        if gw.nowpayments_enabled:
            builder.button(
                text=f"💎 پرداخت {gap} $ از درگاه ارزی",
                callback_data=RenewPartialPayCallback(m="n", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack(),
            )
        if gw.tronado_enabled:
            builder.button(
                text=f"درگاه ترونادو ({gap} $)",
                callback_data=RenewPartialPayCallback(m="tr", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack(),
            )
        if gw.manual_crypto_enabled and gw.manual_crypto_address:
            builder.button(
                text=f"💰 پرداخت {gap} $ به ولت دستی",
                callback_data=RenewPartialPayCallback(m="m", s=sub_id.hex, t=(renew_type[:1] or "v"), a=str(amount)).pack(),
            )
        builder.button(text=Buttons.BACK, callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack())
        builder.adjust(1)
        await safe_edit_or_send(
            callback,
            "💸 <b>کسری موجودی — پرداخت اختلاف</b>\n\n"
            f"هزینه تمدید: <b>{price:.2f} $</b>\n"
            f"موجودی کیف پول: <b>{user.wallet.balance:.2f} $</b>\n"
            f"کسری: <b>{gap} $</b>\n\n"
            "می‌توانی فقط مبلغ اختلاف را از یکی از درگاه‌ها پرداخت کنی. وقتی پرداخت تأیید شد، تمدید به‌صورت خودکار اعمال می‌شود.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        return

    # ─── Distributed Redis lock (prevents double-tap / double-charge) ────────
    lock_key = f"renewal_lock:{callback.from_user.id}:{sub_id}"
    async with distributed_lock(lock_key, ttl_seconds=60) as acquired:
        if not acquired:
            await callback.answer("⛔ تمدید در حال پردازش است — لطفاً صبر کنید.", show_alert=True)
            return

        await state.clear()

        # Pre-flight: probe the panel BEFORE we debit. Saves the user
        # from a "debited then refunded" round-trip when the server is
        # already known to be down.
        try:
            from services.provisioning.manager import ProvisioningManager as _PM
            preflight_ok, preflight_reason = await _PM(session).preflight_check_subscription(sub.id)
        except Exception:
            preflight_ok, preflight_reason = True, None  # don't block the user on a broken probe
        if not preflight_ok:
            try:
                await callback.message.answer(f"⚠️ {preflight_reason or 'سرور در دسترس نیست.'}")
            except Exception:
                pass
            return

        # Send loading message as a NEW message so we can edit it with the result
        try:
            loading_msg = await callback.message.answer("⏳ در حال تمدید...")
        except Exception:
            loading_msg = None

        try:
            # Delete the payment method selection message
            try:
                await callback.message.delete()
            except Exception:
                pass

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
            sub.order = order
            await session.flush()

            # Deduct from wallet using WalletManager if price > 0
            from services.wallet.manager import WalletManager, InsufficientBalanceError
            wallet_manager = WalletManager(session)
            if price > 0:
                try:
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
                except InsufficientBalanceError:
                    error_text = "❌ موجودی کیف پول کافی نیست یا درخواست تکراری است."
                    if loading_msg:
                        try:
                            await loading_msg.edit_text(error_text)
                        except Exception:
                            await callback.message.answer(error_text)
                    else:
                        await callback.message.answer(error_text)
                    return

            # Apply renewal — if X-UI sync fails, the exception will propagate
            try:
                await _apply_renewal(sub, renew_type, amount, session)
            except Exception as exc:
                logger.error("Renewal X-UI sync failed for sub %s: %s", sub.id, exc, exc_info=True)
                # Refund the wallet debit since renewal was not applied on the panel
                if price > 0:
                    await wallet_manager.process_transaction(
                        user_id=user.id,
                        amount=price,
                        transaction_type="refund",
                        direction="credit",
                        currency="USD",
                        reference_type="order",
                        reference_id=order.id,
                        description="Refund: renewal failed (panel unreachable)",
                        metadata={"sub_id": str(sub.id), "error": str(exc)[:200]},
                    )
                order.status = "failed"
                error_text = (
                    "❌ خطا در اعمال تمدید روی سرور!\n\n"
                    "ارتباط با پنل X-UI برقرار نشد. مبلغ تمدید به کیف پول شما برگردانده شد.\n"
                    "لطفاً بعداً دوباره تلاش کنید."
                )
                if loading_msg:
                    try:
                        await loading_msg.edit_text(error_text)
                    except Exception:
                        await callback.message.answer(error_text)
                else:
                    await callback.message.answer(error_text)
                return

            # Clear alert dedup keys so user gets re-notified in next cycle
            await _clear_sub_alert_keys(sub.id)

            success_text = Messages.RENEWAL_SUCCESS
            if loading_msg:
                try:
                    await loading_msg.edit_text(success_text)
                except Exception:
                    await callback.message.answer(success_text)
            else:
                await callback.message.answer(success_text)

            # Notify admins (passing `subscription=sub` enables the
            # polished sectioned format that includes server / config name).
            await _notify_renewal_admins(callback, user, renew_type, amount, price, session, subscription=sub)
            
        except Exception as general_exc:
            logger.error("Unexpected crash in renew_pay_wallet: %s", general_exc, exc_info=True)
            crash_text = f"❌ خطای سیستمی رخ داد:\n`{str(general_exc)[:200]}`"
            if loading_msg:
                try:
                    await loading_msg.edit_text(crash_text, parse_mode="Markdown")
                except Exception:
                    pass
            raise  # Re-raise to let middleware rollback DB!



@router.callback_query(RenewPayCallback.filter(F.m == "n"))
async def renew_pay_nowpay(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with NOWPayments gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
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


@router.callback_query(RenewPayCallback.filter(F.m == "t"))
async def renew_pay_tetrapay(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal with TetraPay gateway."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
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
        reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_web, bot_url=tx.payment_url_bot),
    )


@router.callback_query(RenewPayCallback.filter(F.m == "tr"))
async def renew_pay_tronado(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return

    sub_id = rd["sub_id"]
    renew_type = rd["renew_type"]
    amount = rd["amount"]
    price = rd["price"]
    user = rd["user"]

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


@router.callback_query(RenewPayCallback.filter(F.m == "m"))
async def renew_pay_manual(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay renewal via manual crypto — redirect to manual topup flow."""
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return

    price = rd["price"]

    # Store the topup amount and redirect to manual crypto handler
    await state.update_data(topup_amount=str(price))

    from apps.bot.handlers.user.topup import topup_pay_manual
    await topup_pay_manual(callback, state, session)


@router.callback_query(RenewPayCallback.filter(F.m == "c"))
async def renew_pay_card(
    callback: CallbackQuery,
    callback_data: RenewPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Pay a renewal via card-to-card.

    Creates a `direct_renewal` card payment (so the renewal is applied
    automatically on approval, exactly like the gateway renewals) and reuses
    the purchase flow's card-receipt handler — the user uploads the receipt,
    an admin approves, and process_successful_payment renews the sub.
    """
    if callback.from_user is None:
        return
    await callback.answer()

    rd = await _get_renewal_data(callback_data, session, callback.from_user.id)
    if rd is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return

    user = rd["user"]
    price = rd["price"]

    settings_repo = AppSettingsRepository(session)
    gw = await settings_repo.get_gateway_settings()
    from services.card_payments import pick_card, compute_unique_toman
    card = pick_card(gw)
    if not gw.card_to_card_enabled or card is None:
        await safe_edit_or_send(callback, "پرداخت کارت به کارت در حال حاضر فعال نیست.")
        return

    toman_rate = await settings_repo.get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        await safe_edit_or_send(callback, "❌ نرخ تبدیل تومان تنظیم نشده.")
        return

    base_toman = int((price * toman_rate).quantize(Decimal("1")))
    # Per-payer unique amount so the operator can auto-confirm by exact amount.
    toman_amount = await compute_unique_toman(session, base_toman, gw.card_amount_jitter_toman)

    from models.payment import Payment
    payment = Payment(
        user_id=user.id,
        provider="card_to_card",
        kind="direct_renewal",
        order_id=str(uuid4()),
        payment_status="waiting_receipt",
        pay_currency="IRT",
        price_currency="USD",
        price_amount=price,
        pay_amount=Decimal(toman_amount),
        callback_payload={
            "purpose": "renewal",
            "sub_id": str(rd["sub_id"]),
            "renew_type": rd["renew_type"],
            "renew_amount": rd["amount"],
            "card_number": card["number"],
            "card_holder": card["holder"],
            "card_bank": card.get("bank"),
            "base_toman": base_toman,
            "jittered": toman_amount != base_toman,
        },
    )
    session.add(payment)
    await session.flush()

    # Reuse the purchase card-receipt flow (state + handler are kind-agnostic).
    from apps.bot.states.purchase import PurchaseStates
    await state.set_state(PurchaseStates.waiting_for_card_receipt)
    await state.update_data(card_payment_id=str(payment.id))

    card_lines = [
        "💵 پرداخت کارت به کارت (تمدید سرویس)",
        "",
        f"مبلغ <b>دقیق</b>: <b>{toman_amount:,}</b> تومان",
        f"شماره کارت: <code>{card['number']}</code>",
        f"نام صاحب کارت: {card['holder']}",
    ]
    if card.get("bank"):
        card_lines.append(f"بانک: {card['bank']}")
    if toman_amount != base_toman:
        card_lines.append("⚠️ لطفاً <b>دقیقاً</b> همین مبلغ را واریز کن (مبلغ مخصوص حساب توست).")
    if gw.card_note:
        card_lines.extend(["", gw.card_note])
    card_lines.extend(["", "بعد از پرداخت، عکس رسید را همینجا ارسال کنید."])
    await safe_edit_or_send(callback, "\n".join(card_lines), parse_mode="HTML")


# ─── Partial-payment routes (wallet + gateway split) ─────────────────────────
#
# These mirror the regular `renew_pay_*` handlers but:
#   * The gateway invoice is for (renewal_cost - wallet_balance), not the
#     full renewal cost.
#   * The Payment's `callback_payload` carries `partial=True` and
#     `total_renew_cost=<full_cost>` so `services.payment._handle_direct_renewal`
#     debits the FULL renewal cost against the wallet (drains the existing
#     balance + the just-credited gateway portion).


async def _partial_setup(
    callback_data: RenewPartialPayCallback,
    session: AsyncSession,
    user_telegram_id: int,
) -> tuple | None:
    """Pull the renewal data, compute wallet/gateway split, return everything
    the per-provider handler needs. None if invalid."""
    # Reuse the existing pricing helper to avoid drift between the two paths.
    rd = await _get_renewal_data(
        RenewPayCallback(m=callback_data.m, s=callback_data.s, t=callback_data.t, a=callback_data.a),
        session, user_telegram_id,
    )
    if rd is None:
        return None
    user = rd["user"]
    if user.wallet is None:
        return None
    full_cost = Decimal(str(rd["price"]))
    balance = user.wallet.balance or Decimal("0")
    gap = (full_cost - balance).quantize(Decimal("0.01"))
    if gap <= 0:
        # Wallet actually has enough — fall back to regular wallet path.
        return None
    return {**rd, "full_cost": full_cost, "gap": gap, "wallet_portion": balance}


def _partial_meta(rd: dict) -> dict:
    return {
        "purpose": "renewal",
        "partial": True,
        "sub_id": str(rd["sub_id"]),
        "renew_type": rd["renew_type"],
        "renew_amount": rd["amount"],
        "total_renew_cost": float(rd["full_cost"]),
        "wallet_portion": float(rd["wallet_portion"]),
    }


@router.callback_query(RenewPartialPayCallback.filter(F.m == "n"))
async def renew_pay_partial_nowpay(
    callback: CallbackQuery,
    callback_data: RenewPartialPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return
    await callback.answer()
    setup = await _partial_setup(callback_data, session, callback.from_user.id)
    if setup is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return
    user = setup["user"]
    await state.clear()

    from core.config import settings
    from models.payment import Payment
    from schemas.internal.nowpayments import NowPaymentsPaymentCreateRequest
    from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError

    local_order_id = str(uuid4())
    payload_req = NowPaymentsPaymentCreateRequest(
        price_amount=setup["gap"],
        price_currency="usd",
        order_id=local_order_id,
        order_description=f"Partial renewal gap for user {user.id}",
        ipn_callback_url=settings.nowpayments_ipn_callback_url,
    )
    gw = await AppSettingsRepository(session).get_gateway_settings()
    from pydantic import SecretStr
    effective_api_key = SecretStr(gw.nowpayments_api_key) if gw.nowpayments_api_key else settings.nowpayments_api_key

    try:
        async with NowPaymentsClient(NowPaymentsClientConfig(
            api_key=effective_api_key, base_url=settings.nowpayments_base_url,
        )) as client:
            invoice = await client.create_payment_invoice(payload_req)
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
        price_amount=setup["gap"],
        invoice_url=str(invoice.invoice_url),
        callback_payload=_partial_meta(setup),
    )
    session.add(payment)
    await session.flush()

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    await safe_edit_or_send(
        callback,
        f"🧾 فاکتور پرداخت اختلاف ساخته شد:\n\n"
        f"💰 مبلغ گیت‌وی: {setup['gap']} USD\n"
        f"🪙 از کیف پول: {setup['wallet_portion']} USD\n"
        f"📦 جمع تمدید: {setup['full_cost']} USD\n\n"
        "بعد از پرداخت و تایید، تمدید به‌صورت خودکار اعمال می‌شود.",
        reply_markup=build_topup_link_keyboard(str(invoice.invoice_url)),
    )


@router.callback_query(RenewPartialPayCallback.filter(F.m == "t"))
async def renew_pay_partial_tetrapay(
    callback: CallbackQuery,
    callback_data: RenewPartialPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return
    await callback.answer()
    setup = await _partial_setup(callback_data, session, callback.from_user.id)
    if setup is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return
    user = setup["user"]
    await state.clear()

    from core.config import settings
    from models.payment import Payment
    from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError

    toman_rate = await AppSettingsRepository(session).get_toman_rate()
    if not toman_rate or toman_rate <= 0:
        await safe_edit_or_send(callback, "❌ نرخ تبدیل تومان تنظیم نشده.")
        return
    toman_amount = int((setup["gap"] * toman_rate).quantize(Decimal("1")))
    rial_amount = toman_amount * 10
    if rial_amount < 10000:
        await safe_edit_or_send(callback, "❌ مبلغ اختلاف کمتر از حداقل مجاز درگاه است.")
        return

    local_order_id = str(uuid4())
    gw = await AppSettingsRepository(session).get_gateway_settings()
    effective_key = gw.tetrapay_api_key if gw.tetrapay_api_key else settings.tetrapay_api_key.get_secret_value()

    try:
        async with TetraPayClient(TetraPayClientConfig(
            api_key=effective_key, base_url=settings.tetrapay_base_url,
        )) as client:
            tx = await client.create_order(
                hash_id=local_order_id,
                amount=rial_amount,
                description=f"تمدید (اختلاف) - کاربر {user.telegram_id}",
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
        price_amount=setup["gap"],
        pay_amount=Decimal(toman_amount),
        invoice_url=tx.payment_url_bot,
        callback_payload=_partial_meta(setup),
    )
    session.add(payment)
    await session.flush()

    from apps.bot.keyboards.inline import build_topup_link_keyboard
    await safe_edit_or_send(
        callback,
        f"🔖 فاکتور پرداخت اختلاف (ریالی):\n\n"
        f"💵 مبلغ درگاه: {toman_amount:,} تومان\n"
        f"🪙 از کیف پول: {setup['wallet_portion']} USD\n"
        f"📦 جمع تمدید: {setup['full_cost']} USD\n\n"
        "بعد از پرداخت و تایید، تمدید به‌صورت خودکار اعمال می‌شود.",
        reply_markup=build_topup_link_keyboard(invoice_url=tx.payment_url_web, bot_url=tx.payment_url_bot),
    )


@router.callback_query(RenewPartialPayCallback.filter(F.m == "tr"))
async def renew_pay_partial_tronado(
    callback: CallbackQuery,
    callback_data: RenewPartialPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    if callback.from_user is None:
        return
    await callback.answer()
    setup = await _partial_setup(callback_data, session, callback.from_user.id)
    if setup is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return
    user = setup["user"]
    from apps.bot.keyboards.inline import build_topup_link_keyboard
    from services.tronado.payments import create_tronado_invoice

    try:
        invoice = await create_tronado_invoice(
            session=session,
            user=user,
            amount_usd=setup["gap"],
            kind="direct_renewal",
            description=f"Partial renewal gap sub {setup['sub_id']}",
            callback_payload=_partial_meta(setup),
        )
    except Exception as exc:
        await safe_edit_or_send(callback, f"خطا در ساخت فاکتور ترونادو: {exc}")
        return

    await state.clear()
    await safe_edit_or_send(
        callback,
        (
            "فاکتور پرداخت اختلاف ترونادو ساخته شد.\n\n"
            f"مبلغ درگاه: {setup['gap']} USD\n"
            f"از کیف پول: {setup['wallet_portion']} USD\n"
            f"جمع تمدید: {setup['full_cost']} USD\n"
            f"مقدار پرداخت: {invoice.tron_amount} TRX\n\n"
            "بعد از پرداخت و تایید، تمدید به‌صورت خودکار اعمال می‌شود."
        ),
        reply_markup=build_topup_link_keyboard(invoice.invoice_url),
    )


@router.callback_query(RenewPartialPayCallback.filter(F.m == "m"))
async def renew_pay_partial_manual(
    callback: CallbackQuery,
    callback_data: RenewPartialPayCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Manual-crypto partial payment: redirect through topup_pay_manual with
    the GAP amount. Reusing topup_pay_manual keeps the receipt UX consistent
    with non-renewal manual top-ups."""
    if callback.from_user is None:
        return
    await callback.answer()
    setup = await _partial_setup(callback_data, session, callback.from_user.id)
    if setup is None:
        await safe_edit_or_send(callback, "❌ اطلاعات تمدید نامعتبر است.")
        return
    await state.update_data(topup_amount=str(setup["gap"]))
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


async def _notify_renewal_admins(callback, user, renew_type, amount, price, session, *, subscription=None) -> None:
    """Notify about a renewal — prefers the sales report channel.

    Polished sectioned format via services.sales_notifications.notify_renewal.
    If the caller doesn't pass `subscription`, we fall back to the older
    flat string format so we don't break a caller that didn't update.
    """
    if subscription is not None:
        try:
            from services.sales_notifications import notify_renewal as _notify
            await _notify(
                session, callback.bot,
                user=user,
                subscription=subscription,
                renew_type=renew_type,
                amount=float(amount),
                price_usd=price,
                payment_method="wallet",
            )
            return
        except Exception as exc:
            logger.warning("Failed to notify about renewal: %s", exc)
            # fall through to legacy format
    from services.notifications import notify_sales_event
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
        await notify_sales_event(session, callback.bot, admin_text)
    except Exception as exc:
        logger.warning("Failed to notify about renewal (fallback): %s", exc)


async def _clear_sub_alert_keys(sub_id) -> None:
    """Remove all alert dedup keys for a subscription after renewal.

    This ensures the user will be re-notified the next time they approach
    expiry/volume limits (instead of being silenced forever).
    """
    try:
        from core.redis import get_redis
        redis = get_redis()
        pattern = f"alert.sub.{sub_id}.*"
        # Also clear AppSetting-based keys via DB-side delete (done in renewal service)
        # Here we delete any Redis-cached versions
        keys = await redis.keys(pattern)
        if keys:
            await redis.delete(*keys)
    except Exception as exc:
        logger.warning("Failed to clear alert keys for sub %s: %s", sub_id, exc)
