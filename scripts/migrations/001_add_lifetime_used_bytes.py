"""
One-off DB migration: add `subscriptions.lifetime_used_bytes`.

Background
----------
Before this change, every renewal (services/renewal.py:82) and every
inbound migration (services/provisioning/manager.py:743) reset
`Subscription.used_bytes` to 0 — wiping the pre-reset consumption.
Resellers then see a "total delivered bytes" number that drops by
the pre-renewal usage every cycle, and the operator silently
under-bills themselves.

This script:
  1) Adds the new BIGINT column `lifetime_used_bytes` (default 0).
  2) Backfills `lifetime_used_bytes = used_bytes` for every existing
     row so the historical floor isn't zero — at minimum we know
     each subscription has consumed `used_bytes` of traffic right
     now, even if we can't reconstruct what was lost in past resets.

Safe to run multiple times: the ALTER uses `IF NOT EXISTS`, and the
UPDATE only runs on rows where lifetime_used_bytes is still its
default (0). This means re-runs don't double-count.

How to run
----------
On the production VPS:

    docker compose -f docker-compose.prod.yml run --rm api \
        python scripts/add_lifetime_used_bytes.py

Or via volume-mount (if you haven't rebuilt the image yet):

    docker compose -f docker-compose.prod.yml run --rm \
        -v "$(pwd)/scripts:/app/scripts:ro" \
        api python scripts/add_lifetime_used_bytes.py

Exit code is 0 unless the DB connection itself fails.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory


logger = logging.getLogger("add_lifetime_used_bytes")


async def _migrate() -> tuple[bool, int]:
    """Return (added_column, backfilled_rows)."""
    async with AsyncSessionFactory() as session:
        # 1) Add the column. PG 9.6+ supports `IF NOT EXISTS` on ADD COLUMN.
        await session.execute(text("""
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS lifetime_used_bytes BIGINT NOT NULL DEFAULT 0
        """))

        # Detect whether the column was newly added in this run by checking
        # if every row's lifetime_used_bytes is still 0. (If a previous
        # run already backfilled, those rows will have non-zero values
        # AND match used_bytes, so we won't double-update them.)
        result = await session.execute(text("""
            UPDATE subscriptions
            SET lifetime_used_bytes = used_bytes
            WHERE lifetime_used_bytes = 0
              AND used_bytes > 0
        """))
        backfilled = result.rowcount or 0

        await session.commit()
        return True, backfilled


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("add_lifetime_used_bytes: starting")
    try:
        added, backfilled = asyncio.run(_migrate())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1

    if added:
        logger.info("✅ Column subscriptions.lifetime_used_bytes is present (or was just added).")
    logger.info("✅ Backfilled %d row(s)  (lifetime_used_bytes ← used_bytes for rows that were still 0).", backfilled)
    logger.info("")
    logger.info("From this point forward, services/renewal.py and "
                "services/provisioning/manager.py will accumulate into "
                "lifetime_used_bytes before zeroing used_bytes — so the "
                "reseller billing total no longer drops on each renewal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
