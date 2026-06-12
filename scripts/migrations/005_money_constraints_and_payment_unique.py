"""
One-off DB migration: apply the money-safety CHECK constraints from
migrations/012_money_and_inventory_constraints.sql and the webhook-replay
partial UNIQUE index from migrations/011_restore_provider_payment_unique.sql.

Background
----------
The repo-root `migrations/*.sql` files are manual/operational history — NO
deploy path executes them (deploy.sh runs only `init_database` create_all
plus `scripts/migrations/*.py`; the alembic branch is dead because no
alembic.ini exists). That meant every fresh install and every auto-deployed
update silently lacked:

  * the 12 defence-in-depth CHECK constraints on wallets /
    wallet_transactions / discount_codes / plan_inventories (012), and
  * the partial UNIQUE index `uq_payments_provider_payment_id_automated`
    on payments(provider, provider_payment_id) for automated providers
    (011) that blocks a replayed gateway IPN from inserting duplicate
    payment rows.

This script mirrors those statements verbatim so the regular deploy loop
converges fresh AND existing installs. The .sql files stay untouched as the
documented source; see their headers for the design notes (reseller
negative balances down to -credit_limit, the immutable ledger's
balance_after, the sales_limit=0 "unlimited" sentinel, manual crypto rows
legitimately reusing a TX hash).

Safety
------
* Idempotent: every constraint is guarded by a pg_constraint existence
  check, the index by pg_indexes + IF NOT EXISTS; re-runs are a no-op.
* Per-constraint isolation: each ALTER TABLE runs in its own transaction.
  If legacy rows violate one invariant (or a table predates the model),
  only that constraint is skipped — with a warning naming it — and the
  rest still apply.
* The unique index follows scripts/migrations/004's pattern: pre-existing
  duplicates are detected and reported instead of failing the CREATE.
* Always exits 0 on data conflicts — defence-in-depth must never block a
  deploy. Only infrastructure errors (DB unreachable) exit non-zero.

How to run (handled automatically by deploy.sh, or manually):
    docker compose -f docker-compose.prod.yml run --rm api \
        python scripts/migrations/005_money_constraints_and_payment_unique.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory


logger = logging.getLogger("money_constraints_payment_unique")

# (constraint_name, table, CHECK expression) — mirrored verbatim from
# migrations/012_money_and_inventory_constraints.sql.
_CHECK_CONSTRAINTS: list[tuple[str, str, str]] = [
    # Wallet live balance: respect credit_limit. A reseller with
    # credit_limit=10 may legitimately have balance=-10.
    ("ck_wallets_balance_within_credit_limit", "wallets", "balance >= -credit_limit"),
    ("ck_wallets_credit_limit_non_negative", "wallets", "credit_limit >= 0"),
    ("ck_wallets_hold_balance_non_negative", "wallets", "hold_balance >= 0"),
    # Wallet transactions: amount is always positive; direction is a small
    # enum. balance_after is deliberately NOT constrained (immutable ledger
    # records legitimate negative reseller balances).
    ("ck_wallet_tx_amount_positive", "wallet_transactions", "amount > 0"),
    ("ck_wallet_tx_direction", "wallet_transactions", "direction IN ('credit', 'debit')"),
    # Discount codes
    ("ck_discount_percent_range", "discount_codes", "discount_percent BETWEEN 0 AND 100"),
    ("ck_discount_max_uses_positive", "discount_codes", "max_uses >= 1"),
    ("ck_discount_used_count_non_negative", "discount_codes", "used_count >= 0"),
    ("ck_discount_used_not_exceed_max", "discount_codes", "used_count <= max_uses"),
    # Plan inventory: 0 sales_limit means unlimited.
    ("ck_plan_inv_sold_non_negative", "plan_inventories", "sold_count >= 0"),
    ("ck_plan_inv_limit_non_negative", "plan_inventories", "sales_limit >= 0"),
    (
        "ck_plan_inv_sold_not_exceed_limit",
        "plan_inventories",
        "sales_limit <= 0 OR sold_count <= sales_limit",
    ),
]

# Partial unique index mirrored verbatim from
# migrations/011_restore_provider_payment_unique.sql — webhook-replay guard
# scoped to automated providers only; manual rows may still duplicate.
_INDEX_NAME = "uq_payments_provider_payment_id_automated"
_INDEX_PREDICATE = (
    "provider_payment_id IS NOT NULL "
    "AND provider IN ('nowpayments', 'tetrapay', 'tronado')"
)


async def _apply_check_constraints() -> tuple[int, int, list[str]]:
    """Return (created, already_present, skipped_warnings)."""
    created = 0
    present = 0
    warnings: list[str] = []
    for name, table, expression in _CHECK_CONSTRAINTS:
        # One session per constraint so a failed ALTER (legacy rows already
        # violating the invariant) can't poison the remaining statements.
        async with AsyncSessionFactory() as session:
            exists = await session.scalar(
                text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
                {"n": name},
            )
            if exists:
                present += 1
                continue
            try:
                await session.execute(text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"
                ))
                await session.commit()
                created += 1
            except Exception as exc:
                # Most likely: existing rows violate the invariant (the very
                # corruption these constraints exist to catch). Skip just
                # this one; the operator reconciles the data, then re-runs.
                msg = (
                    f"{table}: could not add {name} "
                    f"(existing rows may violate it) — {exc}"
                )
                logger.warning("⚠️  %s", msg)
                warnings.append(msg)
    return created, present, warnings


async def _apply_payment_unique_index() -> tuple[str, list[str]]:
    """Return (status, duplicate_keys)."""
    async with AsyncSessionFactory() as session:
        # Already present? no-op.
        exists = await session.scalar(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
            {"n": _INDEX_NAME},
        )
        if exists:
            return "exists", []

        # Any pre-existing duplicate automated-provider payments that would
        # violate the index? (Same non-destructive stance as 004: report,
        # don't delete, don't block the deploy.)
        dup_rows = await session.execute(text(
            f"""
            SELECT provider || '/' || provider_payment_id, COUNT(*) AS c
            FROM payments
            WHERE {_INDEX_PREDICATE}
            GROUP BY provider, provider_payment_id
            HAVING COUNT(*) > 1
            """
        ))
        duplicates = [r[0] for r in dup_rows.all()]
        if duplicates:
            return "duplicates", duplicates

        await session.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
            f"ON payments (provider, provider_payment_id) "
            f"WHERE {_INDEX_PREDICATE}"
        ))
        await session.commit()
        return "created", []


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("money_constraints_payment_unique: starting")
    try:
        created, present, warnings = asyncio.run(_apply_check_constraints())
        index_status, duplicates = asyncio.run(_apply_payment_unique_index())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1

    logger.info("")
    logger.info("Money-safety constraints summary:")
    logger.info("  added:   %d CHECK constraint(s)", created)
    logger.info("  present: %d already in place", present)
    logger.info("  skipped: %d (see warnings above)", len(warnings))

    if index_status == "exists":
        logger.info("✅ Index %s already present — nothing to do.", _INDEX_NAME)
    elif index_status == "created":
        logger.info(
            "✅ Created partial UNIQUE index %s (webhook-replay guard).", _INDEX_NAME
        )
    elif index_status == "duplicates":
        logger.warning(
            "⚠️  NOT creating %s: found %d duplicated automated payment key(s). "
            "Reconcile these manually, then re-run. Offending provider/payment "
            "ids: %s",
            _INDEX_NAME, len(duplicates), ", ".join(duplicates),
        )
    # Always exit 0 on data conflicts — never block a deploy on this
    # defence-in-depth step. Infrastructure failures returned 1 above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
