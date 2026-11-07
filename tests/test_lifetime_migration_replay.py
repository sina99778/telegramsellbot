"""Run the legacy migration in disposable pre-009 PostgreSQL schemas."""

import os
from collections.abc import AsyncIterator
from importlib import import_module
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.subscription import Subscription
from services.subscription_usage import display_usage

pytestmark = pytest.mark.skipif(
    not os.getenv("AUDIT_TEST_DATABASE_URL"),
    reason="Disposable PostgreSQL URL not configured",
)


@pytest_asyncio.fixture
async def migration_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = "lifetime_migration_" + uuid4().hex
    engine = create_async_engine(os.environ["AUDIT_TEST_DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = create_async_engine(
            os.environ["AUDIT_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            yield async_sessionmaker(scoped, expire_on_commit=False)
        finally:
            await scoped.dispose()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_fresh_lifetime_column_defaults_zero_without_copying_current_usage(
    monkeypatch: pytest.MonkeyPatch,
    migration_sessions: async_sessionmaker[AsyncSession],
) -> None:
    migration = import_module("scripts.migrations.001_add_lifetime_used_bytes")
    monkeypatch.setattr(migration, "AsyncSessionFactory", migration_sessions)
    async with migration_sessions() as session:
        await session.execute(text("CREATE TABLE subscriptions (used_bytes BIGINT NOT NULL)"))
        await session.execute(text("INSERT INTO subscriptions VALUES (20)"))
        await session.commit()

    assert await migration._migrate() == (True, 0)
    assert await migration._migrate() == (True, 0)

    async with migration_sessions() as session:
        assert (await session.execute(text(
            "SELECT used_bytes, lifetime_used_bytes FROM subscriptions"
        ))).one() == (20, 0)
        column = (await session.execute(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'subscriptions' "
            "AND column_name = 'lifetime_used_bytes'"
        ))).one()
        assert column == ("bigint", "NO", "0")


async def test_migration_replay_preserves_renewal_display_and_lifetime_history(
    monkeypatch: pytest.MonkeyPatch,
    migration_sessions: async_sessionmaker[AsyncSession],
) -> None:
    migration = import_module("scripts.migrations.001_add_lifetime_used_bytes")
    monkeypatch.setattr(migration, "AsyncSessionFactory", migration_sessions)
    async with migration_sessions() as session:
        await session.execute(text(
            "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, "
            "used_bytes BIGINT NOT NULL, lifetime_used_bytes BIGINT NOT NULL DEFAULT 0, "
            "cycle_start_used_bytes BIGINT, volume_bytes BIGINT NOT NULL)"
        ))
        await session.execute(text(
            "INSERT INTO subscriptions VALUES (1, 20, 0, 20, 40), (2, 20, 7, NULL, 40)"
        ))
        await session.commit()

    for _ in range(2):
        await migration._migrate()
        async with migration_sessions() as session:
            rows = (await session.execute(text(
                "SELECT id, used_bytes, lifetime_used_bytes, cycle_start_used_bytes, "
                "volume_bytes FROM subscriptions ORDER BY id"
            ))).mappings().all()
            renewed = Subscription(
                used_bytes=rows[0]["used_bytes"],
                lifetime_used_bytes=rows[0]["lifetime_used_bytes"],
                cycle_start_used_bytes=rows[0]["cycle_start_used_bytes"],
                volume_bytes=rows[0]["volume_bytes"],
            )
            usage = display_usage(renewed)
            assert (usage.used_bytes, usage.total_bytes) == (0, 20)
            assert renewed.used_bytes + renewed.lifetime_used_bytes == 20
            assert tuple(dict(rows[0]).values()) == (1, 20, 0, 20, 40)
            assert tuple(dict(rows[1]).values()) == (2, 20, 7, None, 40)
