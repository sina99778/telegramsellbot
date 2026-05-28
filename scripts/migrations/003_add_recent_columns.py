"""
One-off DB migration: explicit safety-net for the four columns added
across recent feature commits, in case the 999_auto_sync_columns
generic sweeper missed any of them.

Columns:
  * subscriptions.user_note          VARCHAR(256) NULL
  * plans.ip_limit                   INTEGER NULL
  * plans.renewal_price_per_gb       NUMERIC(18,8) NULL
  * plans.renewal_price_per_day      NUMERIC(18,8) NULL

Without these, every `select(Subscription)` / `select(Plan)` issued by
the bot or the mini-app fails with UndefinedColumn, which surfaces to
the user as an empty config list in the mini-app and a generic
"خطای ناشناخته" elsewhere.

All ALTERs use `IF NOT EXISTS`, so this script is safe to re-run.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory


logger = logging.getLogger("003_add_recent_columns")


_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS user_note VARCHAR(256)",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS ip_limit INTEGER",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS renewal_price_per_gb NUMERIC(18,8)",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS renewal_price_per_day NUMERIC(18,8)",
)


async def _run() -> int:
    async with AsyncSessionFactory() as session:
        for stmt in _STATEMENTS:
            logger.info("Applying: %s", stmt)
            await session.execute(text(stmt))
        await session.commit()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(_run())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
