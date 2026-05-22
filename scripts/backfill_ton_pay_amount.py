"""
One-off backfill: re-quantize TON `pay_amount` values on stuck
manual-crypto invoices to the new 6-decimal precision.

Background
----------
Before the fix in services/crypto_autoconfirm.py, the TON branch of
quantize_for_currency used 9 decimals (`Decimal("0.000000001")`) but
the `Payment.pay_amount` column is `Numeric(18, 8)`. PostgreSQL silently
rounded the 9th digit away on INSERT, so the value the user was told to
send (rendered with `:.9f`) did NOT equal the value the autoconfirm
worker compared blockchain transactions against → match always failed →
auto-confirm never fired.

After the fix:
  * Display + storage + matching all happen at 6 dp.
  * For invoices in flight, banker's-rounding in `Decimal.quantize`
    means the old DB value usually rounds to the *same* 6-dp number the
    blockchain TX rounds to. Most rows therefore just start working
    without any data change. This script normalizes the column anyway
    so future precision tooling sees consistent values.

How to run
----------
On the production VPS:
    docker compose -f docker-compose.prod.yml run --rm api \
        python scripts/backfill_ton_pay_amount.py

The script is idempotent — re-running shows "updated: 0".
A `--dry-run` flag is supported for inspection without writing.

Exit code is 0 unless the DB connection itself fails.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from sqlalchemy import select

from core.database import AsyncSessionFactory
from models.payment import Payment
from services.crypto_autoconfirm import TON_CURRENCIES, quantize_for_currency


logger = logging.getLogger("backfill_ton_pay_amount")


_WAITING_STATUSES = ("waiting_hash", "waiting_receipt")


async def _backfill(dry_run: bool) -> tuple[int, int, int]:
    """Return (examined, updated, unchanged)."""
    async with AsyncSessionFactory() as session:
        rows = await session.execute(
            select(Payment).where(
                Payment.provider == "manual_crypto",
                Payment.payment_status.in_(_WAITING_STATUSES),
                Payment.pay_currency.in_(tuple(TON_CURRENCIES)),
                Payment.pay_amount.is_not(None),
            )
        )
        payments = list(rows.scalars().all())

        examined = len(payments)
        updated = 0
        unchanged = 0

        for p in payments:
            try:
                old = Decimal(p.pay_amount)
            except Exception:
                logger.warning("payment %s: pay_amount %r not a Decimal — skipping", p.id, p.pay_amount)
                unchanged += 1
                continue

            new = quantize_for_currency(old, p.pay_currency or "TON")
            if new == old:
                unchanged += 1
                continue

            logger.info(
                "payment %s order=%s  %s → %s  (currency=%s, status=%s)",
                p.id, p.order_id, old, new, p.pay_currency, p.payment_status,
            )
            if not dry_run:
                p.pay_amount = new
                updated += 1
            else:
                # In dry-run mode we still count it as "would update" so
                # the operator gets a real preview.
                updated += 1

        if not dry_run:
            await session.commit()
        else:
            await session.rollback()

        return examined, updated, unchanged


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill TON pay_amount precision.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write changes — just print what would be updated.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    label = "DRY-RUN" if args.dry_run else "LIVE"
    logger.info("backfill_ton_pay_amount: starting (%s)", label)

    try:
        examined, updated, unchanged = asyncio.run(_backfill(args.dry_run))
    except Exception as exc:
        logger.error("Backfill failed: %s", exc, exc_info=True)
        return 1

    logger.info("")
    logger.info("Summary:  examined: %d  updated: %d  unchanged: %d  (%s)",
                examined, updated, unchanged, label)
    if examined == 0:
        logger.info("(no TON manual-crypto invoices in waiting_hash/waiting_receipt — nothing to do.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
