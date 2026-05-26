"""
Generic schema-sync — add any model column missing from the DB.

What it does
------------
Introspects every SQLAlchemy mapped table in `models.*` and, for each
column the model declares but PostgreSQL doesn't have, emits an
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` statement.

Why
---
We don't run Alembic, and `create_all` only creates missing TABLES,
never missing COLUMNS. Every time someone adds a new column to a
model on an already-populated DB, the next deploy crashes with
`UndefinedColumnError` until someone hand-writes a migration script.

This script runs last in the migrations folder (999_*) so any
explicit migration that does proper backfill or type-coercion runs
first. After those, this catches any column they forgot.

Safety rules (deliberate omissions)
-----------------------------------
* ONLY adds columns. NEVER drops, renames, or changes type — those
  are destructive and shouldn't be automated.
* ONLY adds nullable columns OR columns with a `server_default`.
  Anything that would require a NOT NULL without default is reported
  as a warning, not added, because adding it would either fail
  (DB has rows) or break the model contract (rows with NULL on a
  non-null column).
* No-op when the schema is already in sync. Safe to re-run on every
  deploy.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import sys
from typing import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.types import TypeEngine

from core.database import AsyncSessionFactory, Base


logger = logging.getLogger("auto_sync_columns")


def _import_all_models() -> None:
    """Make sure every models.* submodule is imported.

    `Base.metadata` only knows about tables whose `Base` subclass has
    actually been imported; in production a stray model registered via
    a never-imported file would be invisible here. Walk `models` and
    import each submodule once.
    """
    import models as _root  # noqa: F401  (anchor for the walker)
    for mod in pkgutil.walk_packages(_root.__path__, prefix="models."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # pragma: no cover — operator visibility
            logger.warning("Could not import %s: %s", mod.name, exc)


def _column_ddl(table_name: str, column) -> str:
    """Render a single `ADD COLUMN IF NOT EXISTS` clause for one Column.

    Honours nullable + server_default. Defaults to NULL when the column
    is nullable and has no explicit default.
    """
    # Compile the column's type using PostgreSQL dialect so we get
    # "JSONB" rather than the generic "JSON", etc.
    col_type: TypeEngine = column.type
    type_sql = col_type.compile(dialect=pg_dialect.dialect())

    parts = [f"ADD COLUMN IF NOT EXISTS {column.name} {type_sql}"]
    if not column.nullable:
        parts.append("NOT NULL")
    if column.server_default is not None:
        # server_default can be a TextClause; rely on its str() rendering
        # (e.g. `text("0")` → "0").
        default = str(column.server_default.arg)  # type: ignore[attr-defined]
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


async def _missing_columns_for_table(
    session, table_name: str, model_columns: Iterable
) -> list:
    """Return Column objects that the model defines but DB doesn't."""
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table_name},
    )
    existing = {r[0] for r in rows.all()}
    missing = [c for c in model_columns if c.name not in existing]
    return missing


async def _sync() -> tuple[int, int, list[str]]:
    """Return (tables_scanned, columns_added, warnings)."""
    _import_all_models()
    warnings: list[str] = []
    added_count = 0

    async with AsyncSessionFactory() as session:
        # Confirm the table exists first — we don't auto-create tables
        # here (init_database already did that). We only add columns to
        # tables that already exist.
        table_rows = await session.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ))
        existing_tables = {r[0] for r in table_rows.all()}

        tables = list(Base.metadata.sorted_tables)
        for tbl in tables:
            if tbl.name not in existing_tables:
                # Will be created by init_database on next deploy; nothing to sync.
                continue
            missing = await _missing_columns_for_table(session, tbl.name, tbl.columns)
            for col in missing:
                if not col.nullable and col.server_default is None:
                    msg = (
                        f"{tbl.name}.{col.name}: model says NOT NULL with no "
                        "server_default — refusing to auto-add. Write an "
                        "explicit migration that backfills first."
                    )
                    logger.warning(msg)
                    warnings.append(msg)
                    continue
                ddl = f"ALTER TABLE {tbl.name} {_column_ddl(tbl.name, col)}"
                logger.info("Adding: %s", ddl)
                await session.execute(text(ddl))
                added_count += 1
        await session.commit()
        return len(tables), added_count, warnings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("auto_sync_columns: starting")
    try:
        tables, added, warnings = asyncio.run(_sync())
    except Exception as exc:
        logger.error("auto-sync failed: %s", exc, exc_info=True)
        return 1

    logger.info("")
    logger.info("Schema-sync summary:")
    logger.info("  scanned: %d tables", tables)
    logger.info("  added:   %d column(s)", added)
    if warnings:
        logger.info("  ⚠️  %d column(s) need an explicit migration:", len(warnings))
        for w in warnings:
            logger.info("       - %s", w)
    else:
        logger.info("  warn:    0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
