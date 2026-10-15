"""Add a replay-safe panel desired-state outbox; no financial history is changed."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from core.database import engine


async def migrate() -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            ALTER TABLE subscriptions
              ADD COLUMN IF NOT EXISTS state_version BIGINT NOT NULL DEFAULT 1,
              ADD COLUMN IF NOT EXISTS panel_sync_pending BOOLEAN NOT NULL DEFAULT false,
              ADD COLUMN IF NOT EXISTS panel_sync_error VARCHAR(128),
              ADD COLUMN IF NOT EXISTS panel_sync_attempts BIGINT NOT NULL DEFAULT 0,
              ADD COLUMN IF NOT EXISTS panel_sync_attempted_at TIMESTAMPTZ
        """)
        )
        await connection.execute(
            text("""
            CREATE INDEX IF NOT EXISTS ix_subscriptions_panel_sync_pending
            ON subscriptions (panel_sync_pending)
        """)
        )
        # Repair prior failed bans; local disabled status is still the intent.
        # Increment the ORM version so an old running sync cannot undo this.
        first_run = await connection.scalar(
            text("""
            INSERT INTO app_settings (key, value_json)
            VALUES ('migration.panel_sync_outbox_v1', '{}')
            ON CONFLICT (key) DO NOTHING RETURNING key
        """)
        )
        if first_run:
            await connection.execute(
                text("""
                UPDATE subscriptions SET panel_sync_pending = true,
                    state_version = state_version + 1
                WHERE status = 'disabled' AND panel_sync_pending = false
                  AND EXISTS (SELECT 1 FROM xui_clients
                              WHERE xui_clients.subscription_id = subscriptions.id)
            """)
            )


async def main() -> None:
    try:
        await migrate()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
