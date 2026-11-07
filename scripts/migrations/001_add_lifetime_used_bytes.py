"""Ensure the historical usage column exists without rewriting accounting.

Lifetime usage contains consumption before the current panel counter; total
delivered usage is lifetime_used_bytes + used_bytes. Copying the current counter
into lifetime would count it twice and invalidate renewal display baselines.
Lost historical usage cannot be reconstructed from the current counter.

This migration may run on every deployment. It only adds a missing BIGINT column
with default zero, preserving all existing counters and renewal metadata.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory

logger = logging.getLogger("add_lifetime_used_bytes")


async def _migrate() -> tuple[bool, int]:
    """Return (column_present, rewritten_rows); accounting rows are never rewritten."""
    logger.info("Attempting to connect to the database...")
    async with AsyncSessionFactory() as session:
        logger.info("Connected successfully! Starting ALTER TABLE...")
        # 1) Add the column. PG 9.6+ supports `IF NOT EXISTS` on ADD COLUMN.
        await session.execute(text("""
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS lifetime_used_bytes BIGINT NOT NULL DEFAULT 0
        """))
        logger.info("ALTER TABLE completed; preserving all usage counters.")

        await session.commit()
        logger.info("Commit successful!")
        return True, 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("add_lifetime_used_bytes: starting")
    try:
        present, _ = asyncio.run(_migrate())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1

    if present:
        logger.info("✅ Column subscriptions.lifetime_used_bytes is present (or was just added).")
    logger.info("Existing usage counters and renewal baselines were preserved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
