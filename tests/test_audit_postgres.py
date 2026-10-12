"""Opt-in PostgreSQL races. Set AUDIT_TEST_DATABASE_URL to a disposable DB.

Only a uniquely named temporary schema is created; existing tables are unused.
"""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.crypto_claim import CryptoTransactionClaim
from services.crypto_claims import claim_crypto_transaction
from services.provisioning.manager import ProvisioningManager, ProvisioningError

pytestmark = pytest.mark.skipif(not os.getenv("AUDIT_TEST_DATABASE_URL"), reason="Disposable PostgreSQL URL not configured")


@pytest_asyncio.fixture
async def postgres_sessions():
    schema = "audit_test_" + uuid4().hex
    engine = create_async_engine(os.environ["AUDIT_TEST_DATABASE_URL"])
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        async with scoped.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'CREATE TABLE "{schema}".payments (id UUID PRIMARY KEY)'))
            await conn.run_sync(CryptoTransactionClaim.__table__.create)
        yield async_sessionmaker(scoped, expire_on_commit=False), schema
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_concurrent_crypto_claim_is_atomic_and_survives_payment_delete(postgres_sessions):
    sessions, schema = postgres_sessions
    ids = [uuid4(), uuid4()]
    async with sessions() as session:
        for pid in ids:
            await session.execute(text(f'INSERT INTO "{schema}".payments (id) VALUES (:id)'), {"id": pid})
        await session.commit()

    async def attempt(pid):
        async with sessions() as session:
            payment = SimpleNamespace(id=pid, pay_currency="TRON", provider_payment_id="ABC", callback_payload={})
            try:
                await claim_crypto_transaction(session, payment)
                await session.commit()
                return pid
            except ValueError:
                await session.rollback()
                return None

    results = await asyncio.gather(*(attempt(pid) for pid in ids))
    winners = [pid for pid in results if pid is not None]
    assert len(winners) == 1
    async with sessions() as session:
        await session.execute(text(f'DELETE FROM "{schema}".payments WHERE id=:id'), {"id": winners[0]})
        await session.commit()
    loser = next(pid for pid in ids if pid != winners[0])
    assert await attempt(loser) is None


async def test_server_capacity_lock_is_shared_across_transactions(postgres_sessions):
    sessions, _ = postgres_sessions
    server_id = uuid4()
    async with sessions() as first, sessions() as second:
        await ProvisioningManager(first)._lock_server_capacity(server_id, ProvisioningError)
        with pytest.raises(ProvisioningError):
            await ProvisioningManager(second)._lock_server_capacity(server_id, ProvisioningError)
        await first.rollback()
        await ProvisioningManager(second)._lock_server_capacity(server_id, ProvisioningError)
