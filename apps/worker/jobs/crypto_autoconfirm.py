"""
Worker job that auto-confirms manual-crypto topups.

Runs every 30s. For each pending `manual_crypto` payment on a chain we
support (TRX, USDT-TRC20, TON), it:
  1) pulls recent incoming TXs to the deposit address,
  2) finds one whose amount matches the invoice's exact pay_amount and
     whose timestamp is after the invoice was created,
  3) on match, calls process_successful_payment(...) which credits the
     user's wallet through the normal idempotent code path.

Design notes
------------
* No blockchain API key is required. TronGrid and toncenter both serve
  enough free traffic for a small bot. If volume grows the admin can
  add a key later — no schema migration needed.
* Each successful auto-confirm appends the TX hash to a list on the
  payment row so the same hash never auto-confirms twice (replay
  protection on top of process_successful_payment's own idempotency).
* Each match is wrapped in `session.begin_nested()` (a SAVEPOINT) so a
  failure on one invoice does NOT roll back the matches that already
  succeeded earlier in the same job run.
* After a successful auto-confirm we also:
    - clear the buying user's FSM "waiting_for_manual_hash" state (so
      whatever they type next isn't interpreted as a TX hash);
    - post to the sales-report channel via notify_sales_event.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory
from models.payment import Payment
from services.crypto_autoconfirm import (
    AUTOCONFIRM_CURRENCIES,
    amount_matches,
    fetch_incoming,
    is_autoconfirmable,
)
from services.payment import process_successful_payment

logger = logging.getLogger(__name__)

# Don't try to autoconfirm invoices older than this (anything not paid
# by 24h is almost certainly abandoned; let admin handle the rare late
# arrival manually).
_MAX_INVOICE_AGE = timedelta(hours=24)


async def run_crypto_autoconfirm(session: AsyncSession, bot: Bot | None = None) -> dict:
    """Public entry point used by apps/worker/main.py scheduler."""
    cutoff = datetime.now(timezone.utc) - _MAX_INVOICE_AGE

    rows = await session.execute(
        select(Payment).where(
            Payment.provider == "manual_crypto",
            Payment.payment_status.in_(("waiting_hash", "waiting_receipt")),
            Payment.created_at >= cutoff,
            Payment.pay_currency.in_(tuple(AUTOCONFIRM_CURRENCIES)),
            Payment.pay_amount.is_not(None),
        )
    )
    pending: list[Payment] = list(rows.scalars().all())
    if not pending:
        return {"checked": 0, "confirmed": 0}

    # Group by (currency, address) so we only hit each blockchain API
    # once per unique deposit destination.
    by_target: dict[tuple[str, str], list[Payment]] = {}
    for p in pending:
        payload = p.callback_payload or {}
        addr = payload.get("address")
        cur = (p.pay_currency or "").strip()
        if not addr or not is_autoconfirmable(cur):
            continue
        by_target.setdefault((cur, str(addr)), []).append(p)

    confirmed = 0
    checked = 0
    for (currency, address), invoices in by_target.items():
        # `since` = the OLDEST invoice's created_at — so we catch a TX that
        # may have been sent for either invoice. The amount-equality
        # check then disambiguates which one it actually belongs to.
        since = min((p.created_at for p in invoices if p.created_at), default=cutoff)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # Apply 60s lookback to cover clock skew between our DB and the
        # explorer.
        since_with_skew = since - timedelta(seconds=60)

        txs = await fetch_incoming(currency=currency, address=address, since=since_with_skew)
        checked += len(txs)

        for tx in txs:
            tx_hash = tx.get("hash")
            tx_amount = tx.get("amount")
            tx_ts = tx.get("timestamp")
            if not tx_hash or tx_amount is None or tx_ts is None:
                continue

            matched_payment: Payment | None = None
            for p in invoices:
                if p.pay_amount is None:
                    continue
                # TX must be at least as new as the invoice itself.
                p_created = p.created_at
                if p_created and p_created.tzinfo is None:
                    p_created = p_created.replace(tzinfo=timezone.utc)
                if p_created and tx_ts < p_created - timedelta(seconds=60):
                    continue
                # Don't re-process the same hash on the same payment.
                processed = (p.callback_payload or {}).get("autoconfirm_processed_hashes") or []
                if tx_hash in processed:
                    continue
                if amount_matches(currency, Decimal(p.pay_amount), Decimal(tx_amount)):
                    matched_payment = p
                    break

            if matched_payment is None:
                continue

            # Each match runs inside its own SAVEPOINT so a failure on
            # this invoice (e.g. transient X-UI panel error, audit log
            # FK constraint, …) doesn't roll back any matches that
            # already succeeded earlier in the same job run.
            try:
                async with session.begin_nested():
                    # Stamp hash + processed marker BEFORE
                    # process_successful_payment. process_successful_payment
                    # is itself idempotent (FOR UPDATE + actually_paid
                    # guard); this list is the autoconfirm-specific
                    # replay-protection.
                    payload = dict(matched_payment.callback_payload or {})
                    processed = list(payload.get("autoconfirm_processed_hashes") or [])
                    processed.append(tx_hash)
                    payload["autoconfirm_processed_hashes"] = processed
                    payload["autoconfirm_tx_hash"] = tx_hash
                    payload["autoconfirm_at"] = datetime.now(timezone.utc).isoformat()
                    payload["tx_hash"] = tx_hash  # surface to admin recovery UI
                    matched_payment.provider_payment_id = tx_hash
                    matched_payment.callback_payload = payload
                    await session.flush()

                    await process_successful_payment(
                        session=session,
                        payment=matched_payment,
                        amount_to_credit=Decimal(str(matched_payment.price_amount)),
                    )
            except Exception as exc:
                logger.error(
                    "[CRYPTO-AUTOCONFIRM] process_successful_payment failed for %s: %s",
                    matched_payment.id, exc, exc_info=True,
                )
                # Skip the rest of the post-match steps for this invoice.
                continue

            # If we got here, the SAVEPOINT committed. Remove the
            # matched invoice from the local list so a follow-up TX in
            # the same poll doesn't re-trigger this branch.
            try:
                invoices.remove(matched_payment)
            except ValueError:
                pass
            confirmed += 1
            logger.info(
                "[CRYPTO-AUTOCONFIRM] confirmed payment=%s tx=%s amount=%s %s",
                matched_payment.id, tx_hash, tx_amount, currency,
            )

            # ── Post-match best-effort follow-ups (never failed below)
            # Note: we deliberately do NOT try to clear the buyer's FSM
            # "waiting_for_manual_hash" state from here. The worker runs
            # in its own process and cannot reach the bot's MemoryStorage.
            # The bot-side hash-input handler already short-circuits when
            # it finds the payment row in a terminal state, so a late
            # /skip from the user is harmless.

            # 1) DM the user.
            if bot is not None:
                try:
                    from models.user import User as _U
                    u = await session.scalar(select(_U).where(_U.id == matched_payment.user_id))
                    if u:
                        # Escape values that could contain Telegram-HTML special
                        # chars even though tx hashes are hex; defence in depth.
                        from html import escape as _esc
                        await bot.send_message(
                            u.telegram_id,
                            "✅ <b>پرداخت کریپتو شما به‌صورت خودکار تأیید شد</b>\n"
                            f"💰 موجودی کیف پول شما <b>{matched_payment.price_amount:.2f} $</b> شارژ شد.\n"
                            f"🔗 هش تراکنش: <code>{_esc(str(tx_hash))}</code>",
                            parse_mode="HTML",
                        )
                except Exception as exc:
                    logger.warning("autoconfirm user notify failed: %s", exc)

            # 2) Post to the sales-report channel (or fall back to
            #    admin DMs if no channel is configured).
            if bot is not None:
                try:
                    from services.notifications import notify_sales_event
                    from models.user import User as _U
                    u = await session.scalar(select(_U).where(_U.id == matched_payment.user_id))
                    if u:
                        from html import escape as _esc
                        user_link = (
                            f"@{u.username}" if u.username
                            else f"<a href='tg://user?id={u.telegram_id}'>مشاهده پروفایل</a>"
                        )
                        msg = (
                            "💳 شارژ خودکار کیف پول!\n\n"
                            f"👤 کاربر: {_esc(u.first_name or '-')} | {user_link} "
                            f"(ID: <code>{u.telegram_id}</code>)\n"
                            f"💰 مبلغ: <b>{matched_payment.price_amount:.2f} USD</b>\n"
                            f"💱 ارز: {_esc(currency)}\n"
                            f"🔗 TX: <code>{_esc(str(tx_hash))}</code>"
                        )
                        await notify_sales_event(session, bot, msg)
                except Exception as exc:
                    logger.warning("autoconfirm sales-notify failed: %s", exc)

    return {"checked": checked, "confirmed": confirmed, "pending_invoices": len(pending)}
