# Database Bootstrap Notes

## Current State

The project currently initializes schema objects through SQLAlchemy metadata bootstrap in `core.database.init_database()`.

That means:

- a fresh environment can create tables without a complete Alembic history
- the repository does not yet provide a full migration chain from the first schema revision to the latest revision
- the existing `migrations/` folder should be treated as partial operational history, not a complete upgrade ledger

## Fresh Install Expectation

Fresh installs should use the existing deployment flow, which bootstraps the database directly when a complete Alembic setup is not present.

## Upgrade Expectation

If you already have production data, review the SQL files in `migrations/` before applying them manually. Do not assume that `migrations/` alone is enough to reconstruct every historical schema step.

## Public Repository Guidance

This limitation is documented so operators understand the current support boundary. A future hardening step would be to add a complete Alembic environment and revision history, but that is intentionally outside the scope of this documentation-only release pass.
