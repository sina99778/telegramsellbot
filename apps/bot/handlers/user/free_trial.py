from __future__ import annotations

from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.redis import distributed_lock
from core.texts import Buttons
from apps.bot.utils.menu_match import MenuText
from models.order import Order
from models.plan import Plan
from models.user import User, UserProfile
from repositories.settings import AppSettingsRepository
from repositories.user import UserRepository
from services.phone_verification import get_verified_phone, normalize_phone_number
from services.provisioning.manager import ProvisioningError, ProvisioningManager

router = Router(name="user-free-trial")


@router.callback_query(F.data == "user:free_trial")
async def free_trial_from_callback(callback, session: AsyncSession, bot: Bot) -> None:
    """Adapter for empty-state CTAs that point at the free-trial flow."""
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return

    class _Pseudo:
        from_user = callback.from_user

        async def answer(self, *args, **kwargs):
            return await callback.message.answer(*args, **kwargs)

    await free_trial_handler(_Pseudo(), session, bot)


@router.message(MenuText(Buttons.TEST_CONFIG))
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
        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 خرید سرویس جدید", callback_data="user:buy")
        kb.button(text="📦 سرویس‌های من", callback_data="user:my_configs")
        kb.adjust(1)
        await message.answer(
            "❌ شما قبلاً یک کانفیگ تست دریافت کرده‌اید.\n\n"
            "برای دسترسی پایدار و پرسرعت، یک سرویس عادی تهیه کنید.",
            reply_markup=kb.as_markup(),
        )
        return

    # Dedup by verified phone — a user who deletes and recreates their
    # Telegram account would otherwise be able to claim unlimited trials.
    verified_phone = get_verified_phone(user)
    if verified_phone is not None:
        normalized = normalize_phone_number(verified_phone)
        sibling = await session.scalar(
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(
                User.id != user.id,
                User.has_received_free_trial.is_(True),
                UserProfile.notes.contains(f'"phone": "{normalized}"'),
            )
            .limit(1)
        )
        if sibling is not None:
            await message.answer("کانفیگ تست برای این شماره قبلا صادر شده است.")
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

    # ─── Distributed Redis lock (prevents double-tap double-claim) ──────────
    # Each tap runs in its own asyncio task with its own session, so N rapid
    # taps would all read has_received_free_trial == False and provision N
    # free configs. Serialize the claim per user.
    async with distributed_lock(f"free_trial_lock:{user.id}", ttl_seconds=60) as acquired:
        if not acquired:
            await message.answer("⛔ درخواست کانفیگ تست شما در حال پردازش است — لطفاً صبر کنید.")
            return

        # Re-check the flag INSIDE the lock — a concurrent handler may have
        # claimed the trial and committed between our check above and the
        # lock acquisition.
        await session.refresh(user, attribute_names=["has_received_free_trial"])
        if user.has_received_free_trial:
            await message.answer("❌ شما قبلاً یک کانفیگ تست دریافت کرده‌اید.")
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
        # Commit BEFORE releasing the lock — the middleware otherwise commits
        # only after the handler returns (i.e. after the lock is released),
        # leaving a window where a second tap could acquire the freed lock,
        # still read the flag as False, and claim a second trial.
        await session.commit()

    await message.answer(
        "کانفیگ تست شما آماده است:\n\n"
        f"Sub link:\n{result.sub_link}\n\n"
        f"Config:\n{result.vless_uri}"
    )
