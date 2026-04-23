"""
Payment processing service.
Handles IPN callbacks and direct purchase provisioning.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from models.payment import Payment
from models.plan import Plan
from models.user import User
from services.wallet.manager import WalletManager

logger = logging.getLogger(__name__)


def _get_shared_bot() -> Bot:
    """Get or create a shared Bot instance to avoid creating multiple sessions."""
    return Bot(token=settings.bot_token.get_secret_value())


async def process_successful_payment(
    session: AsyncSession,
    payment: Payment,
    amount_to_credit: Decimal,
) -> None:
    logger.info("[PAYMENT] Processing payment %s (kind=%s, amount=%s)", payment.id, payment.kind, amount_to_credit)

    # Idempotency: skip wallet credit if already done, but still allow provisioning retry
    wallet_already_credited = payment.actually_paid is not None

    if not wallet_already_credited:
        payment.actually_paid = amount_to_credit

        # 1. Top up wallet
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
                "payment_status": payment.payment_status,
            },
        )
        logger.info("[PAYMENT] Wallet credited OK")
    else:
        logger.info("[PAYMENT] Wallet already credited — skipping credit step")

    # 2. If it is direct purchase, attempt provisioning (retriable)
    if payment.kind == "direct_purchase":
        # Check if already provisioned via callback_payload flag
        if payment.callback_payload and payment.callback_payload.get("provisioned"):
            logger.info("[PAYMENT] Already provisioned — skipping")
            return

        logger.info("[PAYMENT] Step 2: Direct purchase — provisioning config")
        try:
            await _handle_direct_purchase(session, payment)
            # Mark as provisioned so retries don't duplicate
            if payment.callback_payload is None:
                payment.callback_payload = {}
            payment.callback_payload["provisioned"] = True
            logger.info("[PAYMENT] Direct purchase provisioning COMPLETED")
        except Exception as exc:
            logger.error("[PAYMENT] Direct purchase provisioning FAILED: %s", exc, exc_info=True)
            # Don't re-raise — wallet was already credited, user can buy manually
            # But do NOT mark as provisioned so next retry can attempt again
    else:
        logger.info("[PAYMENT] Payment kind=%s — not a direct purchase, done", payment.kind)


async def _handle_direct_purchase(
    session: AsyncSession,
    payment: Payment,
) -> None:
    """Provision a subscription after a successful direct purchase payment."""
    purchase_meta = payment.callback_payload
    logger.info("[PROVISION] callback_payload keys: %s", list(purchase_meta.keys()) if purchase_meta else "EMPTY")

    plan_id_str = purchase_meta.get("plan_id") if purchase_meta else None
    if not plan_id_str:
        logger.error("[PROVISION] Missing plan_id in purchase metadata for payment %s", payment.id)
        return

    plan_id = UUID(plan_id_str)
    config_name = purchase_meta.get("config_name", "VPN")
    discount_percent = purchase_meta.get("discount_percent", 0)

    logger.info("[PROVISION] plan_id=%s, config_name=%s, discount=%s", plan_id, config_name, discount_percent)

    # Load user with wallet
    user = await session.scalar(
        select(User)
        .options(selectinload(User.wallet))
        .where(User.id == payment.user_id)
    )
    plan = await session.get(Plan, plan_id)

    if not user:
        logger.error("[PROVISION] User %s not found", payment.user_id)
        return
    if not plan:
        logger.error("[PROVISION] Plan %s not found", plan_id)
        return
    if not user.wallet:
        logger.error("[PROVISION] User %s has no wallet", payment.user_id)
        return

    logger.info("[PROVISION] User: %s (tg=%s), Plan: %s", user.id, user.telegram_id, plan.name)

    original_price = plan.price
    if discount_percent > 0:
        final_price = (original_price * (Decimal(100 - discount_percent) / Decimal(100))).quantize(Decimal("0.01"))
    else:
        final_price = original_price

    from services.provisioning.manager import ProvisioningManager, ProvisioningError
    from models.order import Order
    from core.formatting import format_volume_bytes

    # Consume discount code NOW (after payment confirmed), not at invoice creation
    discount_id_str = purchase_meta.get("discount_id")
    if discount_id_str:
        from repositories.discount import DiscountRepository
        from models.discount import DiscountCode
        dc = await session.get(DiscountCode, UUID(discount_id_str))
        if dc and dc.used_count < dc.max_uses:
            await DiscountRepository(session).use_code(dc)
            logger.info("[PROVISION] Consumed discount code %s", dc.id)

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
    logger.info("[PROVISION] Order created: %s", order.id)

    # Debit from wallet (was credited above)
    wallet_manager = WalletManager(session)
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
    logger.info("[PROVISION] Wallet debited OK")

    # Use a single shared Bot for all messaging
    bot = _get_shared_bot()
    try:
        # Provision
        provisioning_manager = ProvisioningManager(session)
        logger.info("[PROVISION] Calling provision_subscription...")
        provisioned = await provisioning_manager.provision_subscription(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
            config_name=config_name,
        )
        logger.info("[PROVISION] Provisioning SUCCESS — sub_link=%s", provisioned.sub_link[:50] if provisioned.sub_link else "NONE")

        order.status = "provisioned"

        volume_label = format_volume_bytes(plan.volume_bytes)
        sub_link = provisioned.sub_link
        vless_uri = provisioned.vless_uri

        # Send config to user
        text = (
            "✅ کانفیگ شما آماده است!\n\n"
            f"📛 نام: {config_name}\n"
            f"📦 پلن: {plan.name}\n"
            f"💾 حجم: {volume_label}\n"
            f"📅 مدت: {plan.duration_days} روز\n"
            f"💰 پرداخت شده: {final_price:.2f} {plan.currency}\n"
            f"💳 روش: درگاه پرداخت\n"
            f"🕐 فعال‌سازی: از اولین اتصال\n\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🔗 ساب لینک:\n{sub_link}\n\n"
            f"📋 کانفیگ مستقیم:\n{vless_uri}"
        )
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

        # Notify admins
        from services.notifications import notify_admins
        user_link = f"@{user.username}" if user.username else f"<a href='tg://user?id={user.telegram_id}'>مشاهده پروفایل</a>"
        admin_text = (
            "🛒 خرید جدید (درگاه)!\n\n"
            f"👤 کاربر: {user.first_name or '-'} | {user_link} (ID: <code>{user.telegram_id}</code>)\n"
            f"📦 پلن: {plan.name}\n"
            f"💰 مبلغ: {final_price:.2f} {plan.currency}\n"
            f"📛 کانفیگ: {config_name}\n"
            f"💳 روش: درگاه پرداخت"
        )
        try:
            await notify_admins(session, bot, admin_text)
        except Exception as exc:
            logger.warning("[PROVISION] Failed to notify admins: %s", exc)

    except ProvisioningError as exc:
        logger.error("[PROVISION] Provisioning FAILED: %s", exc)
        # Refund
        await wallet_manager.process_transaction(
            user_id=user.id,
            amount=Decimal(str(final_price)),
            transaction_type="refund",
            direction="credit",
            currency=plan.currency,
            reference_type="order",
            reference_id=order.id,
            description="Automatic refund after provisioning failure",
            metadata={"plan_id": str(plan.id)},
        )
        order.status = "refunded"
        try:
            await bot.send_message(
                user.telegram_id,
                "❌ خطا در ساخت کانفیگ. مبلغ به کیف پول شما بازگردانده شد."
            )
        except Exception as bot_exc:
            logger.error("[PROVISION] Failed to send refund message: %s", bot_exc)
    except Exception as exc:
        logger.error("[PROVISION] Failed to send config to user: %s", exc, exc_info=True)
    finally:
        await bot.session.close()

    # ── Referral bonus on first purchase ──
    try:
        await _process_gateway_referral_bonus(session, user)
    except Exception as exc:
        logger.warning("[PROVISION] Referral bonus failed: %s", exc)


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
    order_count = int(
        await session.scalar(
            sel(func.count()).select_from(OrderModel)
            .where(
                OrderModel.user_id == user.id,
                OrderModel.status.in_(["provisioned", "paid", "completed"]),
            )
        ) or 0
    )
    if order_count != 1:
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
