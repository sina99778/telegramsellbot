"""
Payment processing service.
Handles IPN callbacks and direct purchase provisioning.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import UUID

from aiogram.client.default import DefaultBotProperties
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from apps.bot.premium_bot import IPv4AiohttpSession, PremiumEmojiBot
from models.order import Order
from models.payment import Payment
from models.plan import Plan
from models.subscription import Subscription
from models.user import User
from repositories.settings import AppSettingsRepository
from services.custom_purchase import (
    CustomPurchaseError,
    calculate_custom_purchase_price,
    create_custom_purchase_plan,
    get_custom_purchase_template_plan,
)
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig, NowPaymentsRequestError
from services.tetrapay.client import TetraPayClient, TetraPayClientConfig, TetraPayRequestError
from services.tronado.client import TronadoClient, TronadoClientConfig, TronadoRequestError
from services.wallet.manager import WalletManager

logger = logging.getLogger(__name__)


def _get_shared_bot() -> PremiumEmojiBot:
    """Create a temporary Bot instance for sending messages.

    IMPORTANT: Callers MUST close the bot session when done, e.g.:
        bot = _get_shared_bot()
        try:
            await bot.send_message(...)
        finally:
            await bot.session.close()
    """
    return PremiumEmojiBot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
        session=IPv4AiohttpSession(),
    )


async def process_successful_payment(
    session: AsyncSession,
    payment: Payment,
    amount_to_credit: Decimal,
) -> bool:
    if payment.kind != "direct_renewal":
        return await _process_successful_payment(session, payment, amount_to_credit)

    payload = dict(payment.callback_payload or {})
    if payload.get("renewal_applied"):
        return True
    if payload.get("renewal_refused"):
        return False

    sub_id_str = payload.get("sub_id")
    try:
        sub_id = UUID(str(sub_id_str))
    except (TypeError, ValueError):
        logger.error("[RENEWAL] Missing or invalid sub_id for payment %s", payment.id)
        return False

    from core.redis import distributed_lock, renewal_lock_key

    async with distributed_lock(renewal_lock_key(sub_id), ttl_seconds=120) as acquired:
        if not acquired:
            logger.warning(
                "[RENEWAL] Could not acquire renewal lock for sub %s (payment %s) — deferring to retry",
                sub_id_str,
                payment.id,
            )
            return False
        return await _process_successful_payment(
            session,
            payment,
            amount_to_credit,
            renewal_lock_held=True,
        )


async def _process_successful_payment(
    session: AsyncSession,
    payment: Payment,
    amount_to_credit: Decimal,
    *,
    renewal_lock_held: bool = False,
) -> bool:
    """Process a successful payment: credit the wallet and trigger any
    follow-up action (provision config, apply renewal).

    Concurrency contract: callers MUST acquire the row lock on the payment
    before calling this function (every webhook handler and the manual
    approval handler already do so via `SELECT ... FOR UPDATE`). We don't
    re-acquire it here because doing so would clash with mock-backed unit
    tests and is redundant in production.
    """
    logger.info("[PAYMENT] Processing payment %s (kind=%s, amount=%s)", payment.id, payment.kind, amount_to_credit)

    # Idempotency: skip wallet credit if already done, but still allow provisioning retry
    wallet_already_credited = payment.actually_paid is not None

    if not wallet_already_credited:
        # 1. Top up wallet FIRST — the credited markers are written only after
        # this succeeds. The old order (flush actually_paid/"finished" before
        # the credit) permanently branded the payment as credited even when the
        # credit raised: the wallet write lives in its own SAVEPOINT, so its
        # rollback kept the already-flushed markers alive, and swallow-then-
        # commit callers (webhooks, manual approval) committed that half-state
        # — every retry then skipped the credit forever (silent money loss).
        # The flush-early "so concurrent retries see it" rationale was wrong:
        # callers hold FOR UPDATE on the payment row, and uncommitted flushes
        # are invisible across transactions anyway.
        logger.info("[PAYMENT] Step 1: Credit wallet for user %s", payment.user_id)
        wallet_manager = WalletManager(session)
        await wallet_manager.process_transaction(
            user_id=payment.user_id,
            amount=amount_to_credit,
            transaction_type="deposit",
            direction="credit",
            currency=payment.price_currency,
            reference_type="payment",
            reference_id=payment.id,
            description="Automated wallet credit",
            metadata={
                "provider": payment.provider,
                "provider_payment_id": payment.provider_payment_id,
                "payment_status": "finished",
            },
        )
        payment.payment_status = "finished"
        payment.actually_paid = amount_to_credit
        await session.flush()
        logger.info("[PAYMENT] Wallet credited OK")
    else:
        payment.payment_status = "finished"
        logger.info("[PAYMENT] Wallet already credited — skipping credit step")


    # 2. If it is direct purchase, attempt provisioning (retriable)
    if payment.kind == "direct_purchase":
        # Check if already provisioned via callback_payload flag
        if payment.callback_payload and payment.callback_payload.get("provisioned"):
            logger.info("[PAYMENT] Already provisioned — skipping")
            return True

        logger.info("[PAYMENT] Step 2: Direct purchase — provisioning config")
        try:
            provisioned = await _handle_direct_purchase(session, payment)
            if provisioned is not True:
                logger.info("[PAYMENT] Direct purchase was not provisioned; leaving retry flag open")
                return False
            # Mark as provisioned so retries don't duplicate
            payload = dict(payment.callback_payload or {})
            payload["provisioned"] = True
            payment.callback_payload = payload
            logger.info("[PAYMENT] Direct purchase provisioning COMPLETED")
            return True
        except Exception as exc:
            logger.error("[PAYMENT] Direct purchase provisioning FAILED: %s", exc, exc_info=True)
            return False

    # 3. If it is direct renewal, apply renewal automatically
    elif payment.kind == "direct_renewal":
        if payment.callback_payload and payment.callback_payload.get("renewal_applied"):
            logger.info("[PAYMENT] Renewal already applied — skipping")
            return True
        if payment.callback_payload and payment.callback_payload.get("renewal_refused"):
            logger.info("[PAYMENT] Renewal was refused (sub status) — not retrying")
            return False

        logger.info("[PAYMENT] Step 3: Direct renewal — applying renewal")
        try:
            renewed = await _handle_direct_renewal(
                session,
                payment,
                lock_held=renewal_lock_held,
            )
            if renewed is not True:
                logger.info("[PAYMENT] Direct renewal was not applied; leaving retry flag open")
                return False
            payload = dict(payment.callback_payload or {})
            payload["renewal_applied"] = True
            payment.callback_payload = payload
            logger.info("[PAYMENT] Direct renewal COMPLETED")
            return True
        except Exception as exc:
            logger.error("[PAYMENT] Direct renewal FAILED: %s", exc, exc_info=True)
            return False
    else:
        logger.info("[PAYMENT] Payment kind=%s — not a direct purchase/renewal, done", payment.kind)
        # Sales notification for wallet top-up
        try:
            from services.sales_notifications import notify_wallet_topup as _notify_topup
            user = await session.scalar(select(User).where(User.id == payment.user_id))
            if user:
                bot = _get_shared_bot()
                try:
                    tx_hash = (payment.callback_payload or {}).get("tx_hash") or payment.provider_payment_id
                    await _notify_topup(
                        session,
                        bot,
                        user=user,
                        amount_usd=amount_to_credit,
                        payment_method=payment.provider,
                        tx_hash=tx_hash,
                    )
                finally:
                    await bot.session.close()
        except Exception as exc:
            logger.warning("[PAYMENT] Failed to notify sales channel about topup: %s", exc)
        return True


async def _handle_direct_purchase(
    session: AsyncSession,
    payment: Payment,
) -> bool:
    """Provision a subscription after a successful direct purchase payment."""
    purchase_meta = dict(payment.callback_payload or {})
    logger.info("[PROVISION] callback_payload keys: %s", list(purchase_meta.keys()) if purchase_meta else "EMPTY")

    # Load user with wallet
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet))
        .where(User.id == payment.user_id)
    )
    if not user:
        logger.error("[PROVISION] User %s not found", payment.user_id)
        return False
    if not user.wallet:
        logger.error("[PROVISION] User %s has no wallet", payment.user_id)
        return False

    plan_id_str = purchase_meta.get("plan_id")
    plan = None
    if not plan_id_str and purchase_meta.get("custom_purchase"):
        try:
            volume_gb = float(purchase_meta.get("custom_volume_gb") or 0)
            duration_days = int(purchase_meta.get("custom_duration_days") or 0)
            custom_settings = await AppSettingsRepository(session).get_custom_purchase_settings()
            template_plan = await get_custom_purchase_template_plan(session)
            if template_plan is None:
                logger.error("[PROVISION] Missing custom purchase template plan for payment %s", payment.id)
                return False
            calculate_custom_purchase_price(
                custom_settings,
                volume_gb=volume_gb,
                duration_days=duration_days,
            )
            plan = await create_custom_purchase_plan(
                session,
                volume_gb=volume_gb,
                duration_days=duration_days,
                price=Decimal(str(payment.price_amount)),
                template_plan=template_plan,
            )
        except (TypeError, ValueError, CustomPurchaseError) as exc:
            logger.error("[PROVISION] Invalid custom purchase metadata for payment %s: %s", payment.id, exc)
            return False
        purchase_meta["plan_id"] = str(plan.id)
        purchase_meta["custom_plan_created"] = True
        payment.callback_payload = dict(purchase_meta)
        logger.info("[PROVISION] Custom plan created after payment confirmation: %s", plan.id)

    plan_id_str = purchase_meta.get("plan_id")
    if not plan_id_str:
        logger.error("[PROVISION] Missing plan_id in purchase metadata for payment %s", payment.id)
        return False

    plan_id = UUID(plan_id_str)
    config_name = purchase_meta.get("config_name", "VPN")
    discount_percent = purchase_meta.get("discount_percent", 0)

    logger.info("[PROVISION] plan_id=%s, config_name=%s, discount=%s", plan_id, config_name, discount_percent)

    if plan is None:
        plan = await session.get(Plan, plan_id)
    if not plan:
        logger.error("[PROVISION] Plan %s not found", plan_id)
        return False

    logger.info("[PROVISION] User: %s (tg=%s), Plan: %s", user.id, user.telegram_id, plan.name)

    final_price = Decimal(str(payment.price_amount))

    from services.provisioning.manager import ProvisioningManager, ProvisioningError
    from core.formatting import format_volume_bytes

    # Consume discount code NOW (after payment confirmed), not at invoice creation.
    # use_code re-validates inside a row lock and returns None if the code is no
    # longer applicable — in that case we just don't credit the discount.
    # Idempotency: stamp discount_consumed (mirrors the wallet_debited marker) so
    # provisioning RETRIES (IPN re-delivery, recovery, reconciliation — up to 10
    # attempts) don't burn another use of the same code for the same purchase.
    discount_id_str = purchase_meta.get("discount_id")
    if discount_id_str and not purchase_meta.get("discount_consumed"):
        from repositories.discount import DiscountRepository
        from models.discount import DiscountCode
        dc = await session.get(DiscountCode, UUID(discount_id_str))
        if dc is not None:
            consumed = await DiscountRepository(session).use_code(dc)
            if consumed is not None:
                logger.info("[PROVISION] Consumed discount code %s", dc.id)
            else:
                logger.warning("[PROVISION] Discount %s could not be consumed (expired/exhausted)", dc.id)
            # Either way, never retry the consumption for this payment: a
            # successful use must not double-burn, and an exhausted/expired
            # verdict won't change on retry.
            payload = dict(payment.callback_payload or {})
            payload["discount_consumed"] = True
            payment.callback_payload = payload
            purchase_meta = dict(purchase_meta)
            purchase_meta["discount_consumed"] = True
            await session.flush()

    order = None
    existing_order_id = purchase_meta.get("order_id") if purchase_meta else None
    if existing_order_id:
        try:
            order = await session.get(Order, UUID(str(existing_order_id)))
        except ValueError:
            order = None

    debited = bool(purchase_meta.get("wallet_debited")) if purchase_meta else False
    if order is None:
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            status="processing",
            source="gateway",
            amount=final_price,
            currency=plan.currency,
        )
        session.add(order)
        await session.flush()
        payload = dict(payment.callback_payload or {})
        payload["order_id"] = str(order.id)
        payment.callback_payload = payload
        purchase_meta = payload
        logger.info("[PROVISION] Order created: %s", order.id)
    else:
        logger.info("[PROVISION] Reusing order %s for payment %s", order.id, payment.id)

    wallet_manager = WalletManager(session)
    bot = _get_shared_bot()
    provisioning_manager = ProvisioningManager(session)

    try:
        preflight_res = provisioning_manager.preflight_check_plan(plan.id)
        if asyncio.iscoroutine(preflight_res) or hasattr(preflight_res, "__await__"):
            preflight_ok, preflight_reason = await preflight_res
        elif isinstance(preflight_res, tuple) and len(preflight_res) == 2:
            preflight_ok, preflight_reason = preflight_res
        else:
            preflight_ok, preflight_reason = True, None
    except Exception:
        preflight_ok, preflight_reason = True, None
    if not preflight_ok:
        logger.warning(
            "[PROVISION] Preflight failed for payment %s: %s",
            payment.id,
            preflight_reason,
        )
        return False

    try:
        if not debited:
            async with session.begin_nested():
                await wallet_manager.process_transaction(
                    user_id=user.id,
                    amount=Decimal(str(final_price)),
                    transaction_type="purchase",
                    direction="debit",
                    currency=plan.currency,
                    reference_type="order",
                    reference_id=order.id,
                    description=f"Purchase of plan {plan.code}",
                    metadata={"plan_id": str(plan.id), "config_name": config_name},
                )
                provisioned = await provisioning_manager.provision_subscription(
                    user_id=user.id,
                    plan_id=plan.id,
                    order_id=order.id,
                    config_name=config_name,
                )
            payload = dict(payment.callback_payload or {})
            payload["wallet_debited"] = True
            payload["order_id"] = str(order.id)
            payment.callback_payload = payload
            await session.flush()
            logger.info("[PROVISION] Wallet debited OK")
        else:
            logger.info("[PROVISION] Wallet debit already recorded for order %s", order.id)
            provisioned = await provisioning_manager.provision_subscription(
                user_id=user.id,
                plan_id=plan.id,
                order_id=order.id,
                config_name=config_name,
            )

        logger.info("[PROVISION] Provisioning SUCCESS — sub_link=%s", provisioned.sub_link[:50] if provisioned.sub_link else "NONE")

        order.status = "provisioned"
        payload = dict(payment.callback_payload or {})
        payload["provisioned"] = True
        payload["subscription_id"] = str(provisioned.subscription.id)
        payment.callback_payload = payload

        volume_label = format_volume_bytes(plan.volume_bytes)
        sub_link = provisioned.sub_link
        vless_uri = provisioned.vless_uri

        provider_fa = {
            "nowpayments": "درگاه NOWPayments",
            "tetrapay": "درگاه تتراپی",
            "tronado": "درگاه ترونادو",
            "manual_crypto": "پرداخت دستی",
            "card_to_card": "کارت به کارت",
            "wallet": "کیف پول"
        }.get(payment.provider, payment.provider)

        # Send config to user
        text = (
            "✅ کانفیگ شما آماده است!\n\n"
            f"📛 نام: {config_name}\n"
            f"📦 پلن: {plan.name}\n"
            f"💾 حجم: {volume_label}\n"
            f"📅 مدت: {plan.duration_days} روز\n"
            f"💰 پرداخت شده: {final_price:.2f} {plan.currency}\n"
            f"💳 روش: {provider_fa}\n"
            f"🕐 فعال‌سازی: از اولین اتصال\n\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🔗 ساب لینک:\n{sub_link}\n\n"
            f"📋 کانفیگ مستقیم:\n{vless_uri}"
        )
        try:
            await bot.send_message(user.telegram_id, text)
            logger.info("[PROVISION] Config sent to user %s", user.telegram_id)

            # QR Code
            from core.qr import make_qr_bytes
            from aiogram.types import BufferedInputFile
            qr_bytes = make_qr_bytes(vless_uri)
            if qr_bytes:
                await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=BufferedInputFile(qr_bytes, filename="config_qr.png"),
                    caption=f"📷 QR کد کانفیگ {config_name}",
                )
        except Exception as exc:
            logger.error(
                "[PROVISION] Config provisioned but Telegram delivery failed "
                "for user %s: %s — NOT refunding (config remains active and "
                "visible in my-configs)",
                user.telegram_id, exc,
            )

        # Sales notification
        try:
            from services.sales_notifications import notify_purchase as _notify_sale
            await _notify_sale(
                session, bot,
                user=user,
                subscription=provisioned.subscription,
                price_usd=final_price,
                payment_method=payment.provider,
                config_name=config_name,
            )
        except Exception as exc:
            logger.warning("[PROVISION] Failed to notify about purchase: %s", exc)

    except Exception as exc:
        is_provisioning_err = isinstance(exc, ProvisioningError)
        log_fn = logger.error if is_provisioning_err else logger.critical
        log_fn(
            "[PROVISION] Provisioning FAILED (%s): %s",
            type(exc).__name__, exc, exc_info=not is_provisioning_err,
        )
        payload = dict(payment.callback_payload or {})
        was_debited = bool(payload.get("wallet_debited")) or debited
        refund_ok = not was_debited
        if was_debited:
            try:
                await wallet_manager.process_transaction(
                    user_id=user.id,
                    amount=Decimal(str(final_price)),
                    transaction_type="refund",
                    direction="credit",
                    currency=plan.currency,
                    reference_type="order",
                    reference_id=order.id,
                    description=f"Auto-refund: provisioning of plan {plan.code} failed",
                    metadata={
                        "plan_id": str(plan.id),
                        "failure_reason": type(exc).__name__,
                    },
                )
                refund_ok = True
            except Exception as refund_exc:
                logger.critical(
                    "[PROVISION] REFUND FAILED for payment %s: %s",
                    payment.id, refund_exc, exc_info=True,
                )

        if refund_ok:
            order.status = "refunded"
            payload["wallet_debited"] = False
        else:
            order.status = "failed_needs_manual_refund"
            payload["wallet_debited"] = True
            payload["needs_manual_refund"] = True
        payload["order_id"] = str(order.id)
        payload["provisioning_failed"] = True
        payload["failure_reason"] = type(exc).__name__
        payment.callback_payload = payload
        try:
            if refund_ok:
                await bot.send_message(
                    user.telegram_id,
                    "❌ خطا در ساخت کانفیگ. مبلغ پرداختی به کیف پول شما بازگردانده شد.\n"
                    "می‌توانید با همان موجودی دوباره خرید کنید یا با پشتیبانی تماس بگیرید."
                )
            else:
                await bot.send_message(
                    user.telegram_id,
                    "❌ خطا در ساخت کانفیگ. بازگرداندن مبلغ به‌صورت خودکار ناموفق بود؛ "
                    "تیم پشتیبانی به‌زودی موجودی شما را اصلاح می‌کند."
                )
        except Exception as bot_exc:
            logger.error("[PROVISION] Failed to send refund message: %s", bot_exc)
        return False
    finally:
        await bot.session.close()

    # ── Referral bonus on first purchase ──
    try:
        await _process_gateway_referral_bonus(session, user)
    except Exception as exc:
        logger.warning("[PROVISION] Referral bonus failed: %s", exc)
    return True


async def _handle_direct_renewal(
    session: AsyncSession,
    payment: Payment,
    *,
    lock_held: bool = False,
) -> bool:
    """Apply renewal automatically after a successful gateway payment for renewal."""
    renewal_meta = payment.callback_payload
    if not renewal_meta:
        logger.error("[RENEWAL] Missing callback_payload for renewal payment %s", payment.id)
        return False

    sub_id_str = renewal_meta.get("sub_id")
    renew_type = renewal_meta.get("renew_type")
    renew_amount = renewal_meta.get("renew_amount")

    if not sub_id_str or not renew_type or not renew_amount:
        logger.error("[RENEWAL] Missing renewal metadata for payment %s: %s", payment.id, renewal_meta)
        return False

    sub_id = UUID(sub_id_str)
    from core.redis import distributed_lock, renewal_lock_key

    @asynccontextmanager
    async def held_lock():
        yield True

    lock_context = held_lock() if lock_held else distributed_lock(
        renewal_lock_key(sub_id),
        ttl_seconds=120,
    )
    async with lock_context as acquired:
        if not acquired:
            logger.warning(
                "[RENEWAL] Could not acquire renewal lock for sub %s (payment %s) — deferring to retry",
                sub_id_str, payment.id,
            )
            return False

        subscription = await session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.xui_client))
            .where(Subscription.id == sub_id)
            .execution_options(populate_existing=True)
        )
        if subscription is None:
            logger.error("[RENEWAL] Subscription %s not found for renewal payment %s", sub_id_str, payment.id)
            return False

        if subscription.status not in ("active", "pending_activation", "expired"):
            logger.warning(
                "[RENEWAL] Refusing renewal of sub %s with status=%s (payment %s) — funds stay in wallet",
                sub_id_str, subscription.status, payment.id,
            )
            payload = dict(payment.callback_payload or {})
            payload["renewal_refused"] = True
            payload["renewal_refused_status"] = subscription.status
            payment.callback_payload = payload
            bot = _get_shared_bot()
            try:
                user = await session.scalar(select(User).where(User.id == payment.user_id))
                if user:
                    await bot.send_message(
                        user.telegram_id,
                        "⚠️ امکان تمدید این سرویس وجود ندارد (وضعیت سرویس مجاز نیست).\n"
                        "مبلغ پرداختی در کیف پول شما باقی ماند.",
                    )
            finally:
                await bot.session.close()
            return False

        from services.provisioning.manager import ProvisioningManager

        try:
            preflight_ok, preflight_reason = await ProvisioningManager(
                session
            ).preflight_check_subscription(subscription.id)
        except Exception:
            preflight_ok, preflight_reason = True, None
        if not preflight_ok:
            logger.warning(
                "[RENEWAL] Preflight failed for payment %s: %s",
                payment.id,
                preflight_reason,
            )
            return False

        logger.info("[RENEWAL] Applying renewal: sub=%s, type=%s, amount=%s", sub_id_str, renew_type, renew_amount)
        return await _apply_direct_renewal_locked(
            session, payment, subscription, renew_type, renew_amount, sub_id_str
        )


async def _apply_direct_renewal_locked(
    session: AsyncSession,
    payment: Payment,
    subscription: Subscription,
    renew_type: str,
    renew_amount,
    sub_id_str: str,
) -> bool:
    """Debit + apply_renewal under the renewal lock (see _handle_direct_renewal)."""
    from services.renewal import apply_renewal

    wallet_manager = WalletManager(session)
    payload = dict(payment.callback_payload or {})
    debit_amount = Decimal(str(payload.get("total_renew_cost") or payment.price_amount))
    plan: Plan | None = None
    if renew_type == "plan" and subscription.plan_id:
        plan = await session.scalar(select(Plan).where(Plan.id == subscription.plan_id))

    try:
        if not payload.get("wallet_debited"):
            async with session.begin_nested():
                await wallet_manager.process_transaction(
                    user_id=payment.user_id,
                    amount=debit_amount,
                    transaction_type="renewal",
                    direction="debit",
                    currency=payment.price_currency,
                    reference_type="payment",
                    reference_id=payment.id,
                    description=f"Gateway renewal of subscription {subscription.id}",
                    metadata={
                        "sub_id": str(subscription.id),
                        "type": renew_type,
                        "amount": renew_amount,
                        "provider": payment.provider,
                        "partial": bool(payload.get("partial")),
                        "gateway_portion": str(payment.price_amount),
                        "full_renewal_cost": str(debit_amount),
                    },
                )
                await apply_renewal(
                    session=session,
                    subscription=subscription,
                    renew_type=renew_type,
                    amount=float(renew_amount),
                    plan=plan,
                )
            payload["wallet_debited"] = True
            payment.callback_payload = payload
            await session.flush()
        else:
            await apply_renewal(
                session=session,
                subscription=subscription,
                renew_type=renew_type,
                amount=float(renew_amount),
                plan=plan,
            )
        logger.info("[RENEWAL] Renewal applied successfully for subscription %s", sub_id_str)

    except Exception as exc:
        logger.critical(
            "[RENEWAL] apply_renewal FAILED for sub %s (%s): %s",
            sub_id_str, type(exc).__name__, exc, exc_info=True,
        )
        was_debited = bool(payload.get("wallet_debited"))
        refund_ok = not was_debited
        if was_debited:
            try:
                await wallet_manager.process_transaction(
                    user_id=payment.user_id,
                    amount=debit_amount,
                    transaction_type="refund",
                    direction="credit",
                    currency=payment.price_currency,
                    reference_type="payment",
                    reference_id=payment.id,
                    description=f"Auto-refund: renewal of {sub_id_str} failed",
                    metadata={
                        "sub_id": str(subscription.id),
                        "failure_reason": type(exc).__name__,
                    },
                )
                refund_ok = True
            except Exception as refund_exc:
                logger.critical(
                    "[RENEWAL] REFUND ALSO FAILED for payment %s: %s",
                    payment.id, refund_exc, exc_info=True,
                )

        payload = dict(payment.callback_payload or {})
        if refund_ok:
            payload["wallet_debited"] = False
        else:
            payload["wallet_debited"] = True
            payload["needs_manual_refund"] = True
        payload["renewal_failed"] = True
        payload["failure_reason"] = type(exc).__name__
        payment.callback_payload = payload
        
        try:
            bot = _get_shared_bot()
            try:
                user = await session.scalar(select(User).where(User.id == payment.user_id))
                if user:
                    if refund_ok:
                        await bot.send_message(
                            user.telegram_id,
                            "❌ تمدید سرویس ناموفق بود. مبلغ پرداختی به کیف پول شما بازگردانده شد.\n"
                            "می‌توانید با موجودی کیف پول مجدداً اقدام به تمدید کنید یا با پشتیبانی تماس بگیرید.",
                        )
                    else:
                        await bot.send_message(
                            user.telegram_id,
                            "❌ تمدید سرویس ناموفق بود و بازگشت مبلغ به کیف پول با خطا مواجه شد.\n"
                            "لطفاً با پشتیبانی تماس بگیرید تا مبلغ شما پیگیری شود.",
                        )

                from services.notifications import notify_admins
                await notify_admins(
                    session, bot,
                    "🚨 تمدید ناموفق (مبلغ در کیف پول کاربر محفوظ است)\n"
                    f"payment={payment.id}\nsub={sub_id_str}\n"
                    f"amount={payment.price_amount} {payment.price_currency}\n"
                    f"reason: {type(exc).__name__}",
                )
            finally:
                await bot.session.close()
        except Exception:
            pass
            
        return False

    # Notify user
    bot: Bot | None = None
    try:
        bot = _get_shared_bot()
        user = await session.scalar(select(User).where(User.id == payment.user_id))
        if user:
            provider_fa = {
                "nowpayments": "درگاه NOWPayments",
                "tetrapay": "درگاه تتراپی",
                "tronado": "درگاه ترونادو",
                "manual_crypto": "پرداخت دستی",
                "card_to_card": "کارت به کارت",
                "wallet": "کیف پول"
            }.get(payment.provider, payment.provider)
            
            if renew_type == "plan":
                type_label = "تمدید پلن فعلی"
                amount_label = "بازنشانی کامل حجم و زمان پلن"
                tail = "حجم و اعتبار سرویس به مقادیر جدید پلن بازنشانی شد."
            elif renew_type == "volume":
                type_label = "حجم"
                amount_label = f"{renew_amount} گیگابایت"
                tail = "سرویس شما بروزرسانی شد."
            else:
                type_label = "زمان"
                amount_label = f"{int(renew_amount)} روز"
                tail = "سرویس شما بروزرسانی شد."
            await bot.send_message(
                user.telegram_id,
                f"✅ تمدید خودکار اعمال شد!\n\n"
                f"📦 نوع تمدید: {type_label}\n"
                f"📊 مقدار: {amount_label}\n"
                f"💳 روش: {provider_fa}\n\n"
                f"{tail}"
            )
            # Sales report channel notification
            try:
                from services.sales_notifications import notify_renewal as _notify_renewal
                await _notify_renewal(
                    session,
                    bot,
                    user=user,
                    subscription=subscription,
                    renew_type=renew_type,
                    amount=float(renew_amount),
                    price_usd=payment.price_amount,
                    payment_method=payment.provider,
                )
            except Exception as exc:
                logger.warning("[RENEWAL] Failed to notify sales channel about renewal: %s", exc)
    except Exception as exc:
        logger.warning("[RENEWAL] Failed to send renewal notification: %s", exc)
    finally:
        if bot is not None:
            await bot.session.close()
    return True


async def review_gateway_payment(session: AsyncSession, payment: Payment) -> str:
    if payment.provider == "nowpayments":
        return await _review_nowpayments_payment(session, payment)
    if payment.provider == "tetrapay":
        return await _review_tetrapay_payment(session, payment)
    if payment.provider == "tronado":
        return await _review_tronado_payment(session, payment)
    return "unsupported_provider"


async def _review_nowpayments_payment(session: AsyncSession, payment: Payment) -> str:
    gw = await AppSettingsRepository(session).get_gateway_settings()
    api_key = gw.nowpayments_api_key
    from pydantic import SecretStr
    effective_key = SecretStr(api_key) if api_key else settings.nowpayments_api_key

    async with NowPaymentsClient(
        NowPaymentsClientConfig(api_key=effective_key, base_url=settings.nowpayments_base_url)
    ) as client:
        try:
            if payment.provider_payment_id:
                status = await client.get_payment_status(payment.provider_payment_id)
                status_payload = status.model_dump(mode="json")
            elif payment.provider_invoice_id:
                status_payload = await client.get_invoice_status(payment.provider_invoice_id)
                payments = status_payload.get("payments")
                if isinstance(payments, list) and payments:
                    latest = payments[-1]
                    if isinstance(latest, dict):
                        status_payload = {**status_payload, **latest}
            else:
                return "missing_provider_reference"
        except NowPaymentsRequestError as exc:
            logger.warning("NOWPayments review failed for %s: %s", payment.id, exc)
            return "provider_error"

    provider_payment_id = status_payload.get("payment_id") or status_payload.get("id")
    if provider_payment_id and not payment.provider_payment_id:
        payment.provider_payment_id = str(provider_payment_id)

    payment_status = str(status_payload.get("payment_status") or status_payload.get("status") or "").lower()
    if payment_status:
        payment.payment_status = payment_status

    payload = dict(payment.callback_payload or {})
    payload["manual_review"] = status_payload
    payment.callback_payload = payload

    if payment_status not in {"finished", "confirmed"}:
        return "not_paid"

    # "Already processed" must mean the FOLLOW-UP action is done too, not just
    # the wallet credit: a credited-but-unapplied direct_renewal (e.g. apply
    # failed and was refunded, or a crash before apply) must stay reviewable so
    # this path can retry applying it — mirrors the Tronado reviewer below.
    if payment.actually_paid is not None and (
        (payment.kind == "direct_purchase" and payload.get("provisioned"))
        or (
            payment.kind == "direct_renewal"
            and (payload.get("renewal_applied") or payload.get("renewal_refused"))
        )
        or payment.kind not in {"direct_purchase", "direct_renewal"}
    ):
        return "already_processed"

    paid_amount = status_payload.get("price_amount") or payment.price_amount
    await process_successful_payment(
        session=session,
        payment=payment,
        amount_to_credit=Decimal(str(paid_amount)),
    )
    return "processed"


async def _review_tetrapay_payment(session: AsyncSession, payment: Payment) -> str:
    authority = payment.provider_payment_id
    if not authority:
        return "missing_provider_reference"

    gw = await AppSettingsRepository(session).get_gateway_settings()
    api_key = gw.tetrapay_api_key or settings.tetrapay_api_key.get_secret_value()
    async with TetraPayClient(
        TetraPayClientConfig(api_key=api_key, base_url=settings.tetrapay_base_url)
    ) as client:
        try:
            verify_res = await client.verify_payment(authority)
        except TetraPayRequestError as exc:
            logger.warning("TetraPay review failed for %s: %s", payment.id, exc)
            return "provider_error"

    payload = dict(payment.callback_payload or {})
    payload["manual_review"] = verify_res.model_dump(mode="json")
    payment.callback_payload = payload

    if str(verify_res.status) != "100":
        payment.payment_status = "failed"
        return "not_paid"

    payment.payment_status = "finished"
    # Same follow-up-aware guard as the NowPayments reviewer above: a credited
    # direct_renewal whose apply never happened must remain retryable.
    if payment.actually_paid is not None and (
        (payment.kind == "direct_purchase" and payload.get("provisioned"))
        or (
            payment.kind == "direct_renewal"
            and (payload.get("renewal_applied") or payload.get("renewal_refused"))
        )
        or payment.kind not in {"direct_purchase", "direct_renewal"}
    ):
        return "already_processed"

    await process_successful_payment(
        session=session,
        payment=payment,
        amount_to_credit=payment.price_amount,
    )
    return "processed"


async def _review_tronado_payment(session: AsyncSession, payment: Payment) -> str:
    if not payment.order_id:
        return "missing_provider_reference"

    gw = await AppSettingsRepository(session).get_gateway_settings()
    api_key = gw.tronado_api_key or settings.tronado_api_key.get_secret_value()
    async with TronadoClient(
        TronadoClientConfig(api_key=api_key, base_url=settings.tronado_base_url)
    ) as client:
        try:
            status_res = await client.get_status_by_payment_id(payment.order_id)
        except TronadoRequestError as exc:
            logger.warning("Tronado review failed for %s: %s", payment.id, exc)
            return "provider_error"

    if str(status_res.PaymentID or "").strip() != payment.order_id:
        return "provider_reference_mismatch"
    if payment.pay_address and status_res.Wallet and status_res.Wallet != payment.pay_address:
        return "provider_reference_mismatch"

    payload = dict(payment.callback_payload or {})
    payload["manual_review"] = status_res.model_dump(mode="json")
    payment.callback_payload = payload

    if not status_res.IsPaid:
        payment.payment_status = str(status_res.OrderStatusTitle or "not_paid").lower()
        return "not_paid"

    if status_res.Hash:
        payment.provider_payment_id = status_res.Hash
    payment.payment_status = "finished"
    if (
        payment.actually_paid is not None
        and (
            (payment.kind == "direct_purchase" and payload.get("provisioned"))
            or (
                payment.kind == "direct_renewal"
                and (payload.get("renewal_applied") or payload.get("renewal_refused"))
            )
            or payment.kind not in {"direct_purchase", "direct_renewal"}
        )
    ):
        return "already_processed"

    await process_successful_payment(
        session=session,
        payment=payment,
        amount_to_credit=payment.price_amount,
    )
    return "processed"


async def _process_gateway_referral_bonus(
    session: AsyncSession,
    user: User,
) -> None:
    """Credit referral bonus after a successful gateway purchase (first purchase only)."""
    from sqlalchemy import func, select as sel
    from repositories.settings import AppSettingsRepository

    settings_repo = AppSettingsRepository(session)
    ref_settings = await settings_repo.get_referral_settings()

    if not ref_settings.enabled:
        return

    if user.referred_by_user_id is None:
        return

    from models.order import Order as OrderModel
    # Count only purchase-shaped orders (renewals are created directly with
    # status="completed", trials carry source="trial") — see the twin filter
    # in apps/bot/handlers/user/purchase.py::_process_referral_bonus.
    order_count = int(
        await session.scalar(
            sel(func.count()).select_from(OrderModel)
            .where(
                OrderModel.user_id == user.id,
                OrderModel.status.in_(["provisioned", "paid"]),
                OrderModel.source != "trial",
            )
        ) or 0
    )
    if order_count != 1:
        return

    # Idempotency guard against concurrent first-purchase paths (mirrors the
    # wallet-purchase twin in apps/bot/handlers/user/purchase.py): the bonus
    # is paid at most once per referee, ever.
    from models.wallet import WalletTransaction

    # Serialize concurrent gateway IPNs for the same referee: a plain SELECT
    # COUNT is not enough — two callbacks can both read already_paid == 0 before
    # either commits and both credit the referrer. Lock the referrer's user row
    # first so the second caller blocks until the first commits, then re-reads
    # the guard and bails.
    await session.execute(
        sel(User.id).where(User.id == user.referred_by_user_id).with_for_update()
    )
    already_paid = await session.scalar(
        sel(func.count()).select_from(WalletTransaction).where(
            WalletTransaction.user_id == user.referred_by_user_id,
            WalletTransaction.type == "referral_bonus",
            WalletTransaction.reference_type == "referral",
            WalletTransaction.reference_id == user.id,
        )
    )
    if already_paid:
        return

    wallet_manager = WalletManager(session)
    bot = _get_shared_bot()

    try:
        # Credit referrer
        if ref_settings.referrer_bonus_usd > 0:
            await wallet_manager.process_transaction(
                user_id=user.referred_by_user_id,
                amount=Decimal(str(ref_settings.referrer_bonus_usd)),
                transaction_type="referral_bonus",
                direction="credit",
                currency="USD",
                reference_type="referral",
                reference_id=user.id,
                description=f"Referral bonus for inviting user {user.telegram_id}",
                metadata={"referred_user_id": str(user.id)},
            )
            try:
                referrer = await session.get(User, user.referred_by_user_id)
                if referrer:
                    await bot.send_message(
                        referrer.telegram_id,
                        f"🎉 تبریک! کاربری که دعوت کرده بودید اولین خرید خود را انجام داد.\n"
                        f"💰 {ref_settings.referrer_bonus_usd:.2f} دلار به کیف پول شما اضافه شد!",
                    )
            except Exception as exc:
                logger.warning("[REFERRAL] Failed to notify referrer: %s", exc)

        # Credit referee
        if ref_settings.referee_bonus_usd > 0:
            await wallet_manager.process_transaction(
                user_id=user.id,
                amount=Decimal(str(ref_settings.referee_bonus_usd)),
                transaction_type="referral_bonus",
                direction="credit",
                currency="USD",
                reference_type="referral",
                reference_id=user.referred_by_user_id,
                description="Referral welcome bonus",
                metadata={"referrer_user_id": str(user.referred_by_user_id)},
            )
            try:
                await bot.send_message(
                    user.telegram_id,
                    f"🎁 خوش آمدید! به خاطر عضویت از طریق لینک دعوت، "
                    f"{ref_settings.referee_bonus_usd:.2f} دلار به کیف پول شما اضافه شد!",
                )
            except Exception as exc:
                logger.warning("[REFERRAL] Failed to notify referee: %s", exc)
    finally:
        await bot.session.close()
