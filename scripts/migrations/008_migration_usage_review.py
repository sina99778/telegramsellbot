"""Park ambiguous legacy-usage corrections without reducing paid entitlements."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from core.database import engine


async def migrate() -> None:
    async with engine.begin() as connection:
        await connection.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS "
            "migration_usage_review_reason VARCHAR(128)"
        ))


async def main() -> None:
    try:
        await migrate()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
