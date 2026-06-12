from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from core.database import AsyncSessionFactory
from models.payment import Payment
from services.payment import review_gateway_payment

logger = logging.getLogger(__name__)

# Gateway-native pending statuses (NowPayments vocabulary; TetraPay/Tronado
# invoices also start at "waiting").
PENDING_GATEWAY_STATUSES = ("waiting", "confirming", "partially_paid", "sending")

# Tronado's unpaid review/webhook branch overwrites payment_status with the
# provider's FREE-FORM OrderStatusTitle (or the "not_paid" fallback) — see
# services/payment.py::_review_tronado_payment — which is never in
# PENDING_GATEWAY_STATUSES, so after one unpaid tick the payment would drop
# out of this sweep forever and a missed IPN could never be recovered.
# Tronado rows are therefore swept by EXCLUSION: any status outside this
# terminal/handled set is still pending and must keep being polled.
TRONADO_TERMINAL_STATUSES = (
    "finished",       # paid + processed
    "expired",        # reconciliation expired the abandoned invoice
    "failed",         # admin recovery marked it failed
    "refunded",       # admin recovery refunded it
    "rejected",       # admin rejected it
    "manual_review",  # reconciliation escalated it to a human
)

# Don't poll abandoned Tronado invoices forever — same 48h horizon as the
# reconciliation job's RETRY_MAX_AGE and the stale waiting_hash cleanup below.
TRONADO_SWEEP_MAX_AGE = timedelta(hours=48)


def _is_sweepable(payment: Payment) -> bool:
    """True while this sweep should still poll the payment's provider."""
    if payment.payment_status in PENDING_GATEWAY_STATUSES:
        return True
    return (
        payment.provider == "tronado"
        and payment.payment_status not in TRONADO_TERMINAL_STATUSES
    )


async def sync_pending_payments() -> None:
    # First, collect candidate payment IDs WITHOUT a lock — just to know what to
    # look at this tick.
    tronado_cutoff = datetime.now(timezone.utc) - TRONADO_SWEEP_MAX_AGE
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(Payment.id).where(
                or_(
                    and_(
                        Payment.payment_status.in_(list(PENDING_GATEWAY_STATUSES)),
                        Payment.provider.in_(["nowpayments", "tetrapay", "tronado"]),
                    ),
                    # Tronado-by-exclusion (see TRONADO_TERMINAL_STATUSES),
                    # time-bounded so abandoned invoices age out of the sweep.
                    and_(
                        Payment.provider == "tronado",
                        Payment.payment_status.notin_(list(TRONADO_TERMINAL_STATUSES)),
                        Payment.created_at >= tronado_cutoff,
                    ),
                )
            )
        )
        payment_ids = [row[0] for row in result.all()]

    # Process each one in its OWN locked transaction. `process_successful_payment`
    # (reached via review_gateway_payment) documents a hard contract: the caller
    # MUST hold a row lock so it can't race the provider IPN webhook and
    # double-credit the wallet. We honor it here with `with_for_update(
    # skip_locked=True)` — if an IPN is already processing this row, we skip it
    # and let the IPN finish. We lock+commit per row (not per batch) so the lock
    # is held only across that one payment's provider HTTP call, never blocking
    # IPNs for unrelated payments.
    for pid in payment_ids:
        async with AsyncSessionFactory() as session:
            payment = await session.scalar(
                select(Payment).where(Payment.id == pid).with_for_update(skip_locked=True)
            )
            if payment is None:
                # Locked by a concurrent IPN, or no longer exists — skip.
                continue
            if not _is_sweepable(payment):
                # Status changed between listing and locking (e.g. the IPN
                # finished it). Nothing to do.
                continue
            try:
                result_text = await review_gateway_payment(session, payment)
                logger.info("Payment sync review %s -> %s", payment.id, result_text)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Unexpected error syncing payment %s: %s", pid, exc, exc_info=True)

    # Cleanup stale manual crypto payments (waiting_hash > 48h). The admin
    # approve handler accepts waiting_hash payments of ANY age and holds the
    # row FOR UPDATE across process_successful_payment, so an unlocked
    # read-modify-write here would queue behind that lock and then overwrite
    # the just-approved payment's 'finished' status with 'expired'
    # (last-writer-wins). Same two-phase pattern as the sweep above: list
    # candidate ids without a lock, then expire each row in its own locked
    # transaction with the eligibility re-checked under the lock.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    async with AsyncSessionFactory() as session:
        stale_result = await session.execute(
            select(Payment.id).where(
                Payment.payment_status == "waiting_hash",
                Payment.created_at < cutoff,
            )
        )
        stale_ids = [row[0] for row in stale_result.all()]

    for pid in stale_ids:
        async with AsyncSessionFactory() as session:
            try:
                payment = await session.scalar(
                    select(Payment)
                    .where(
                        Payment.id == pid,
                        # Re-check under the lock — an admin may have
                        # approved/rejected it between listing and locking.
                        Payment.payment_status == "waiting_hash",
                        # Belt-and-braces: never expire a payment that was
                        # already credited.
                        Payment.actually_paid.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                )
                if payment is None:
                    # Locked by a concurrent admin approval, or already
                    # resolved — skip.
                    continue
                payment.payment_status = "expired"
                logger.info(
                    "Expired stale waiting_hash payment %s (created %s)",
                    payment.id, payment.created_at,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error(
                    "Failed to expire stale waiting_hash payment %s: %s",
                    pid, exc, exc_info=True,
                )
