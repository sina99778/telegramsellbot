"""Keep a renewal-period display baseline; unknown historical rows remain NULL."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from core.database import engine


async def main() -> None:
    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS "
                "cycle_start_used_bytes BIGINT"
            ))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
