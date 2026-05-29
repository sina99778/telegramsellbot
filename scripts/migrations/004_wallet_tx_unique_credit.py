"""
One-off DB migration: add a partial UNIQUE index that prevents double-crediting
a gateway payment into the wallet.

Background
----------
`process_successful_payment` (services/payment.py) credits the wallet once per
payment, guarded by a FOR UPDATE row lock + the `payment.actually_paid` flag.
Two worker jobs used to skip that lock (now fixed), opening a narrow window
where a payment could be credited twice. This index is defence-in-depth: it
turns a second "deposit credit" for the same payment into a hard DB error
instead of silent money loss.

Scope (deliberately tight)
--------------------------
The wallet credit for a gateway payment is the unique tuple:
    type = 'deposit'  AND  direction = 'credit'  AND  reference_type = 'payment'
The index is partial on exactly that predicate, so it NEVER conflicts with:
  * the gateway renewal DEBIT      (type='renewal', direction='debit')
  * provisioning-failure refunds   (type='refund',  reference_type='order')
  * plan-purchase debits           (reference_type='order')
  * admin gifts / dashboard credits (reference_type != 'payment')

Safety
------
* Idempotent: checks pg_indexes before creating; re-runs are a no-op.
* NON-destructive: if pre-existing duplicate credits are found (from the old
  race), it does NOT delete anything (that would desync the ledger from the
  balance) and does NOT create the index — it logs the offending payment ids
  and exits 0 so the deploy is never blocked. The operator can reconcile those
  rows by hand, then re-run.

How to run (handled automatically by deploy.sh, or manually):
    docker compose -f docker-compose.prod.yml run --rm api \
        python scripts/migrations/004_wallet_tx_unique_credit.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory


logger = logging.getLogger("wallet_tx_unique_credit")

_INDEX_NAME = "uq_wallet_tx_payment_deposit_credit"
_PREDICATE = (
    "reference_type = 'payment' AND \"type\" = 'deposit' "
    "AND direction = 'credit' AND reference_id IS NOT NULL"
)


async def _migrate() -> tuple[str, list[str]]:
    """Return (status, duplicate_payment_ids)."""
    async with AsyncSessionFactory() as session:
        # Already present? no-op.
        exists = await session.scalar(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
            {"n": _INDEX_NAME},
        )
        if exists:
            return "exists", []

        # Any pre-existing duplicate credits that would violate the index?
        dup_rows = await session.execute(text(
            f"""
            SELECT reference_id::text, COUNT(*) AS c
            FROM wallet_transactions
            WHERE {_PREDICATE}
            GROUP BY reference_id
            HAVING COUNT(*) > 1
            """
        ))
        duplicates = [r[0] for r in dup_rows.all()]
        if duplicates:
            return "duplicates", duplicates

        # Safe to create. Regular (non-CONCURRENT) so it runs inside the
        # session transaction; wallet_transactions is small enough that the
        # brief lock is a non-issue.
        await session.execute(text(
            f'CREATE UNIQUE INDEX {_INDEX_NAME} '
            f"ON wallet_transactions (reference_id) WHERE {_PREDICATE}"
        ))
        await session.commit()
        return "created", []


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("wallet_tx_unique_credit: starting")
    try:
        status, duplicates = asyncio.run(_migrate())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1

    if status == "exists":
        logger.info("✅ Index %s already present — nothing to do.", _INDEX_NAME)
    elif status == "created":
        logger.info("✅ Created partial UNIQUE index %s (one deposit-credit per payment).", _INDEX_NAME)
    elif status == "duplicates":
        logger.warning(
            "⚠️  NOT creating %s: found %d payment(s) already credited more than "
            "once. Reconcile these manually (refund the extra credit), then re-run. "
            "Offending payment reference_ids: %s",
            _INDEX_NAME, len(duplicates), ", ".join(duplicates),
        )
    # Always exit 0 — never block a deploy on this defence-in-depth step.
    return 0


if __name__ == "__main__":
    sys.exit(main())
