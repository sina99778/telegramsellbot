"""
Auto-renew job.

For every subscription whose owner opted in (`auto_renew_enabled`), this job
extends the service by the plan's duration shortly before it expires, paying
from the user's wallet — so a paying customer never silently loses service.

Scope (MVP): TIME renewal only (keeps the service alive). Volume top-ups stay
manual (the user still gets 90/95% volume warnings). Gated globally by the
operator's `renewals_enabled` flag and per-service by `auto_renew_enabled`.

Accounting mirrors the manual wallet-renew path exactly: create an Order,
debit the wallet, apply_renewal() on the panel, and REFUND if the panel sync
fails. A Redis lock per subscription prevents any double-charge.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory, utcnow
from core.redis import distributed_lock, renewal_lock_key
from models.app_setting import AppSetting
from models.order import Order
from models.subscription import Subscription
from models.user import User
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerRecord
from repositories.settings import AppSettingsRepository
from services.renewal import apply_renewal, calculate_renewal_price
from services.wallet.manager import InsufficientBalanceError, WalletManager

logger = logging.getLogger(__name__)

# Renew when within this window of expiry, and also catch services that just
# expired (some users top up right after, and the grace lets us still save them).
_RENEW_WITHIN = timedelta(hours=24)
_GRACE_AFTER_EXPIRY = timedelta(days=2)


async def run_auto_renew(session: AsyncSession, bot: Bot) -> None:
    settings_repo = AppSettingsRepository(session)
    user_actions = await settings_repo.get_user_actions_settings()
    if not user_actions.renewals_enabled:
        return  # global kill-switch

    now = utcnow()
    window_end = now + _RENEW_WITHIN
    window_start = now - _GRACE_AFTER_EXPIRY

    # Load only the candidate IDs on the shared session. Each subscription is
    # then processed in its OWN session/transaction below — so a rollback on one
    # failing sub cannot expire the shared session and crash every subsequent
    # sub with a greenlet lazy-load error (the old "one bad sub stops the rest"
    # bug — rollback() expires ALL loaded ORM instances).
    id_rows = await session.execute(
        select(Subscription.id).where(
            Subscription.auto_renew_enabled.is_(True),
            Subscription.plan_id.isnot(None),
            Subscription.status.in_(("active", "expired")),
            Subscription.ends_at.isnot(None),
            Subscription.ends_at <= window_end,
            Subscription.ends_at >= window_start,
        )
    )
    sub_ids = [row[0] for row in id_rows.all()]
    if not sub_ids:
        return

    renewal_settings = await settings_repo.get_renewal_settings()
    for sub_id in sub_ids:
        try:
            async with AsyncSessionFactory() as sub_session:
                sub = await sub_session.scalar(
                    select(Subscription)
                    .options(
                        selectinload(Subscription.user).selectinload(User.wallet),
                        selectinload(Subscription.plan),
                        selectinload(Subscription.xui_client)
                        .selectinload(XUIClientRecord.inbound)
                        .selectinload(XUIInboundRecord.server)
                        .selectinload(XUIServerRecord.credentials),
                    )
                    .where(
                        Subscription.id == sub_id,
                        # Re-check eligibility (it may have changed since the scan).
                        Subscription.auto_renew_enabled.is_(True),
                        Subscription.status.in_(("active", "expired")),
                    )
                )
                if sub is None:
                    continue
                await _try_auto_renew(sub_session, bot, sub, renewal_settings)
        except Exception as exc:  # noqa: BLE001 — one bad sub must not stop the rest
            logger.error("auto-renew failed for sub %s: %s", sub_id, exc, exc_info=True)
            # The per-sub session rolls back + closes on its own context exit;
            # the next sub gets a fresh session, so failures don't cascade.


async def _try_auto_renew(session, bot, sub, renewal_settings) -> None:
    user = sub.user
    plan = sub.plan
    if user is None or user.wallet is None or plan is None:
        return
    days = int(getattr(plan, "duration_days", 0) or 0)
    if days <= 0:
        return  # nothing sensible to extend

    price = calculate_renewal_price(
        renew_type="time", amount=float(days), settings=renewal_settings, plan=plan
    )

    # Snapshot the primitives the insufficient-balance notifier needs BEFORE the
    # debit attempt: its failure path rolls back the session, which expires every
    # loaded ORM instance, and touching an expired attribute on an AsyncSession
    # raises MissingGreenlet — the notification would be silently lost.
    notify_args = {
        "sub_id": sub.id,
        "sub_name": (
            sub.xui_client.username
            if (sub.xui_client and sub.xui_client.username)
            else str(sub.id)[:8]
        ),
        "user_id": user.id,
        "telegram_id": user.telegram_id,
        "is_bot_blocked": bool(user.is_bot_blocked),
    }

    # Canonical renewal lock (shared with the bot + mini-app surfaces) so the
    # worker can't auto-renew a sub while the user is manually renewing it.
    lock_key = renewal_lock_key(sub.id)
    async with distributed_lock(lock_key, ttl_seconds=120) as acquired:
        if not acquired:
            return

        # Re-verify the FULL eligibility predicate on fresh data now that we
        # hold the lock: a manual renewal committed between the scan and this
        # point pushes ends_at out of the renewal window, and without this
        # re-check the user would be charged a second time. Column-only select
        # so the eagerly-loaded relationships on `sub` stay intact (a refresh
        # would expire them and break async lazy loads downstream).
        fresh = (
            await session.execute(
                select(
                    Subscription.auto_renew_enabled,
                    Subscription.status,
                    Subscription.ends_at,
                ).where(Subscription.id == sub.id)
            )
        ).one_or_none()
        now = utcnow()
        if (
            fresh is None
            or not fresh.auto_renew_enabled
            or fresh.status not in ("active", "expired")
            or fresh.ends_at is None
            or fresh.ends_at > now + _RENEW_WITHIN
            or fresh.ends_at < now - _GRACE_AFTER_EXPIRY
        ):
            return

        if user.wallet.balance < price:
            await _notify_insufficient(session, bot, price=float(price), **notify_args)
            return

        order = Order(
            user_id=user.id,
            plan_id=sub.plan_id,
            amount=price,
            currency="USD",
            status="completed",
            source="auto_renew",
        )
        session.add(order)
        await session.flush()
        sub.order = order
        await session.flush()

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
                    description=f"Auto-renewal of subscription {sub.id}",
                    metadata={"sub_id": str(sub.id), "type": "time", "auto": True},
                )
            except InsufficientBalanceError:
                # rollback() expires every loaded ORM instance — only the
                # primitives snapshotted above may be used past this point.
                await session.rollback()
                await _notify_insufficient(session, bot, price=float(price), **notify_args)
                return

        try:
            await apply_renewal(
                session=session, subscription=sub, renew_type="time", amount=float(days)
            )
        except Exception as exc:
            logger.error("auto-renew panel sync failed for sub %s: %s", sub.id, exc, exc_info=True)
            if price > 0:
                await wallet_manager.process_transaction(
                    user_id=user.id,
                    amount=price,
                    transaction_type="refund",
                    direction="credit",
                    currency="USD",
                    reference_type="order",
                    reference_id=order.id,
                    description="Refund: auto-renewal failed (panel unreachable)",
                    metadata={"sub_id": str(sub.id), "error": str(exc)[:200]},
                )
            order.status = "failed"
            await session.commit()
            return

        # Persist this renewal before notifying / moving to the next sub.
        await _clear_sub_alert_keys(session, sub.id)
        await session.commit()

    await _notify_success(bot, sub, user, days, float(price))


async def _notify_success(bot, sub, user, days: int, price: float) -> None:
    if user.is_bot_blocked:
        return
    sub_name = (
        sub.xui_client.username if (sub.xui_client and sub.xui_client.username) else str(sub.id)[:8]
    )
    builder = InlineKeyboardBuilder()
    from apps.bot.handlers.user.my_configs import MyConfigCallback
    builder.button(
        text="⚙️ مشاهده سرویس",
        callback_data=MyConfigCallback(action="view", subscription_id=sub.id).pack(),
    )
    builder.adjust(1)
    text = (
        "🔁 <b>تمدید خودکار انجام شد</b>\n\n"
        f"👤 سرویس: {sub_name}\n"
        f"⏰ تمدید: <b>{days} روز</b>\n"
        f"💵 از کیف پول: <b>{price:.2f}$</b>\n\n"
        "سرویس‌تان بدون قطعی فعال ماند. ✅"
    )
    try:
        await bot.send_message(user.telegram_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except TelegramForbiddenError:
        user.is_bot_blocked = True
    except Exception as exc:
        logger.warning("auto-renew success notify failed for %s: %s", user.telegram_id, exc)


async def _notify_insufficient(
    session: AsyncSession,
    bot,
    *,
    sub_id,
    sub_name: str,
    user_id,
    telegram_id: int,
    is_bot_blocked: bool,
    price: float,
) -> None:
    """Tell the user their wallet was too low — at most once per service per
    approach (deduped via an AppSetting key, cleared on any successful renewal).

    Takes primitive snapshots instead of ORM instances: the debit-race caller
    invokes this right after session.rollback(), which expires every loaded
    instance, and reading an expired attribute on an AsyncSession raises
    MissingGreenlet — losing the notification entirely."""
    if is_bot_blocked:
        return
    key = f"alert.sub.{sub_id}.autorenew_low"
    if await session.get(AppSetting, key) is not None:
        return
    session.add(AppSetting(key=key, value_json={"sent_at": utcnow().isoformat()}))
    await session.commit()

    builder = InlineKeyboardBuilder()
    from apps.bot.handlers.user.my_configs import MyConfigCallback
    builder.button(
        text="💳 شارژ کیف پول",
        callback_data="wallet:topup",
    )
    builder.button(
        text="⚙️ مشاهده سرویس",
        callback_data=MyConfigCallback(action="view", subscription_id=sub_id).pack(),
    )
    builder.adjust(1)
    text = (
        "⚠️ <b>تمدید خودکار ناموفق بود</b>\n\n"
        f"👤 سرویس: {sub_name}\n"
        f"💵 هزینه تمدید: <b>{price:.2f}$</b>\n"
        "موجودی کیف پول کافی نبود.\n\n"
        "برای جلوگیری از قطع سرویس، کیف پول را شارژ کنید — دفعه‌ی بعد خودکار تمدید می‌شود."
    )
    try:
        await bot.send_message(telegram_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except TelegramForbiddenError:
        # User blocked the bot — persist the flag with a plain UPDATE (the ORM
        # instance may be expired by the rollback that led us here).
        await session.execute(
            update(User).where(User.id == user_id).values(is_bot_blocked=True)
        )
        await session.commit()
    except Exception as exc:
        logger.warning("auto-renew low-balance notify failed for %s: %s", telegram_id, exc)


async def _clear_sub_alert_keys(session: AsyncSession, sub_id) -> None:
    """Drop the per-subscription alert dedup keys (expiry/volume/auto-renew) so
    the user gets fresh notifications in the next cycle."""
    await session.execute(
        delete(AppSetting).where(AppSetting.key.like(f"alert.sub.{sub_id}.%"))
    )
