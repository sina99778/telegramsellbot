"""
One-off DB migration: add the request-context columns on `audit_logs`.

Background
----------
models/audit.py was extended at some point with five new nullable
columns:

    ip_address   INET
    user_agent   TEXT
    request_path VARCHAR(255)
    before_state JSONB
    after_state  JSONB

…but `init_database()` (SQLAlchemy create_all) only creates missing
TABLES, never missing columns. So on already-populated production DBs
the `audit_logs` table still has the old narrow shape, and every
`AuditLogRepository.log_action(...)` call now blows up with

    column "ip_address" of relation "audit_logs" does not exist

…which cascaded into a session-rollback on every flush after, so the
operator's whole turn (plan edit, bulk gift, manual approval, …)
failed visibly with a `ProgrammingError` trace code.

This script:
  * Runs `ALTER TABLE … ADD COLUMN IF NOT EXISTS` for each of the
    five columns — safe to re-run, no-op once applied.
  * No data backfill: all five are nullable and only populated on
    NEW audit rows.

How to run
----------
On the production VPS this runs automatically as part of
`./deploy.sh full`. To run on its own:

    docker compose -f docker-compose.prod.yml run --rm \
        -v "$(pwd)/scripts:/app/scripts:ro" \
        api python scripts/migrations/002_add_audit_log_columns.py

Exit code 0 unless DB itself fails.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import text

from core.database import AsyncSessionFactory


logger = logging.getLogger("add_audit_log_columns")


_COLUMNS: list[tuple[str, str]] = [
    ("ip_address",   "INET"),
    ("user_agent",   "TEXT"),
    ("request_path", "VARCHAR(255)"),
    ("before_state", "JSONB"),
    ("after_state",  "JSONB"),
]


async def _migrate() -> list[str]:
    """Return the list of columns that were freshly added in this run."""
    added: list[str] = []
    async with AsyncSessionFactory() as session:
        for name, type_decl in _COLUMNS:
            # Check existence first so we know whether we're actually
            # adding it (for the run summary). `ADD COLUMN IF NOT EXISTS`
            # itself is silently idempotent and won't error on re-run.
            existed = await session.scalar(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='audit_logs' AND column_name=:name"
            ), {"name": name})
            await session.execute(text(
                f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {name} {type_decl}"
            ))
            if not existed:
                added.append(name)
        await session.commit()
    return added


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("add_audit_log_columns: starting")
    try:
        added = asyncio.run(_migrate())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1

    if added:
        logger.info("✅ Added column(s) to audit_logs: %s", ", ".join(added))
    else:
        logger.info("✅ All audit_logs columns already present — nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
