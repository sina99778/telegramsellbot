"""Real transactions in a disposable schema; never uses application tables."""

from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from core.database import Base
from models.order import Order
from models.plan import Plan
from models.subscription import Subscription
from models.user import User
from models.wallet import Wallet, WalletTransaction
from models.xui import XUIClientRecord, XUIInboundRecord, XUIServerCredential, XUIServerRecord
from services.panel_sync import request_panel_sync

pytestmark = pytest.mark.skipif(
    not os.getenv("AUDIT_TEST_DATABASE_URL"),
    reason="Disposable PostgreSQL URL not configured",
)


@pytest_asyncio.fixture
async def sessions():
    schema = "upgrade_test_" + uuid4().hex
    engine = create_async_engine(os.environ["AUDIT_TEST_DATABASE_URL"])
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        async with scoped.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(scoped, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def seed(sessions):
    async with sessions() as session:
        user = User(telegram_id=12345)
        user.wallet = Wallet(balance=Decimal("100"))
        plan = Plan(
            code="fixture",
            name="Fixture",
            protocol="vless",
            duration_days=30,
            volume_bytes=1000,
            price=Decimal("10"),
            renewal_price=Decimal("10"),
        )
        server = XUIServerRecord(name="fixture", base_url="https://panel.invalid")
        server.credentials = XUIServerCredential(
            username="fixture", password_encrypted="FAKE-NOT-USED"
        )
        inbound = XUIInboundRecord(server=server, xui_inbound_remote_id=1, protocol="vless")
        sub = Subscription(
            user=user,
            plan=plan,
            status="active",
            volume_bytes=1000,
            used_bytes=300,
            ends_at=datetime.now(UTC) + timedelta(days=30),
        )
        sub.xui_client = XUIClientRecord(
            inbound=inbound,
            client_uuid="fixture",
            xui_client_remote_id="fixture",
            email="fixture",
            username="fixture",
        )
        session.add_all([user, plan, server, sub])
        await session.commit()
        return user.id, sub.id


async def test_failed_miniapp_renewal_rolls_back_real_wallet_debit(sessions):
    from apps.api.routes.miniapp import users
    from schemas.api.miniapp import RenewalRequest
    from services.renewal import RenewalXUISyncError

    user_id, sub_id = await seed(sessions)
    repo = MagicMock()
    repo.get_user_actions_settings = AsyncMock(return_value=NS(renewals_enabled=True))
    repo.get_renewal_settings = AsyncMock(return_value=NS())

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield True

    async with sessions() as session:
        user = await session.scalar(
            select(User).where(User.id == user_id).options(selectinload(User.wallet))
        )
        sub = await session.get(Subscription, sub_id)
        with (
            patch.object(users, "check_rate_limit", AsyncMock()),
            patch.object(users, "_get_user_subscription", AsyncMock(return_value=sub)),
            patch.object(users, "AppSettingsRepository", return_value=repo),
            patch.object(users, "calculate_renewal_price", return_value=Decimal("10")),
            patch.object(
                users, "apply_renewal", AsyncMock(side_effect=RenewalXUISyncError("offline"))
            ),
            patch("core.redis.distributed_lock", lock),
        ):
            with pytest.raises(RenewalXUISyncError):
                await users.renew_subscription(
                    RenewalRequest(
                        subscription_id=sub_id,
                        renew_type="volume",
                        amount=1,
                        payment_method="wallet",
                    ),
                    (user, session),
                )
    async with sessions() as session:
        assert await session.scalar(
            select(Wallet.balance).where(Wallet.user_id == user_id)
        ) == Decimal("100")
        assert await session.scalar(select(func.count()).select_from(WalletTransaction)) == 0
        assert await session.scalar(select(Order.status)) == "failed"


async def test_renewal_intent_is_invisible_until_commit_and_survives_new_session(sessions):
    from services.renewal import apply_renewal

    _, sub_id = await seed(sessions)
    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(return_value=NS(xui_limit_ip=0))
    async with sessions() as writer:
        sub = await writer.get(Subscription, sub_id)
        plan = await writer.get(Plan, sub.plan_id)
        with (
            patch("services.renewal._sync_xui_limits", AsyncMock()) as remote,
        ):
            await apply_renewal(
                session=writer, subscription=sub, renew_type="plan", amount=1, plan=plan
            )
        remote.assert_not_awaited()
        async with sessions() as reader:
            before = await reader.get(Subscription, sub_id)
            assert not before.panel_sync_pending and before.volume_bytes == 1000
        await writer.commit()
    async with sessions() as reader:
        after = await reader.get(Subscription, sub_id)
        assert after.panel_sync_pending and after.volume_bytes - after.used_bytes == 1000
        assert after.used_bytes == 300 and after.lifetime_used_bytes == 0


async def test_stale_usage_writer_cannot_erase_committed_renewal_intent(sessions):
    _, sub_id = await seed(sessions)
    async with sessions() as stale, sessions() as fresh:
        old = await stale.get(Subscription, sub_id)
        current = await fresh.get(Subscription, sub_id)
        current.volume_bytes = 2000
        request_panel_sync(current)
        await fresh.commit()
        old.used_bytes = 400
        with pytest.raises(StaleDataError):
            await stale.commit()
        await stale.rollback()
    async with sessions() as check:
        sub = await check.get(Subscription, sub_id)
        assert sub.panel_sync_pending and sub.volume_bytes == 2000


@pytest.mark.parametrize("failure", ["conflict", "timeout", "panel_error", "success"])
async def test_realtime_read_savepoint_preserves_request_and_fresh_renewal(sessions, failure):
    from apps.worker.jobs import subscriptions
    from schemas.internal.xui import XUIInbound
    from services.xui.client import XUIRequestError

    user_id, sub_id = await seed(sessions)
    security = NS(xui_limit_ip=0, auto_disable_ip_abuse=False, max_distinct_ips=0)
    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(return_value=security)

    async def snapshot():
        if failure == "conflict":
            async with sessions() as writer:
                renewed = await writer.get(Subscription, sub_id)
                renewed.volume_bytes = 2000
                request_panel_sync(renewed)
                await writer.commit()
        return [XUIInbound(
            id=1, protocol="vless",
            settings={"clients": [{"id": "fixture", "email": "fixture"}]},
            clientStats=[{"email": "fixture", "up": 400, "down": 0}],
        )]

    async def ip_probe(*args):
        if failure == "timeout":
            raise asyncio.CancelledError()
        if failure == "panel_error":
            raise XUIRequestError("fixture panel failure")
        return None

    @asynccontextmanager
    async def panel(server):
        yield NS(get_inbounds=AsyncMock(side_effect=snapshot))

    async with sessions() as session:
        user = await session.get(User, user_id)
        user.language_code = "fa"
        sub = await session.scalar(
            select(Subscription).where(Subscription.id == sub_id).options(
                selectinload(Subscription.plan), selectinload(Subscription.xui_client),
            )
        )
        cached_record = sub.xui_client
        with (
            patch.object(subscriptions, "AppSettingsRepository", return_value=repo),
            patch.object(subscriptions, "create_xui_client_for_server", panel),
            patch.object(subscriptions, "_get_ip_abuse", side_effect=ip_probe),
        ):
            if failure in {"timeout", "panel_error"}:
                error = asyncio.CancelledError if failure == "timeout" else XUIRequestError
                with pytest.raises(error):
                    await subscriptions.get_realtime_usage(session, sub)
            else:
                result = await subscriptions.get_realtime_usage(session, sub)
                assert (result is None) is (failure == "conflict")
        assert session.is_active
        # Both the caller's cached references and new queries remain usable.
        assert cached_record is sub.xui_client
        assert cached_record.username == "fixture" and sub.plan.name == "Fixture"
        assert sub.used_bytes == (400 if failure == "success" else 300)
        assert sub.volume_bytes == (2000 if failure == "conflict" else 1000)
        assert sub.panel_sync_pending is (failure == "conflict")
        assert await session.scalar(select(User.language_code)) == "fa"
        await session.commit()
    async with sessions() as check:
        assert await check.scalar(select(User.language_code)) == "fa"
        assert await check.scalar(select(Wallet.balance)) == Decimal("100")
        assert await check.scalar(select(func.count()).select_from(WalletTransaction)) == 0


@pytest.mark.parametrize("conflict_at", ["flush", "commit"])
async def test_usage_worker_retries_fresh_after_concurrent_renewal(sessions, conflict_at):
    from apps.worker.jobs import subscriptions

    _, sub_id = await seed(sessions)
    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(
        return_value=NS(xui_limit_ip=0, restart_xray_on_expiry=False)
    )

    @asynccontextmanager
    async def panel(server):
        yield NS()

    async def concurrent_renewal(session, client, group, settings):
        old = group[0]
        async with sessions() as writer:
            current = await writer.get(Subscription, sub_id)
            current.volume_bytes = 2000
            request_panel_sync(current)
            await writer.commit()
        old.used_bytes = 400
        if conflict_at == "flush":
            await session.flush()
        return False

    session_factory = MagicMock(side_effect=sessions)
    sync = AsyncMock(side_effect=concurrent_renewal)
    with (
        patch.object(subscriptions, "AsyncSessionFactory", session_factory),
        patch.object(subscriptions, "AppSettingsRepository", return_value=repo),
        patch.object(subscriptions, "create_xui_client_for_server", panel),
        patch.object(subscriptions, "sync_xui_usage_and_status", sync),
    ):
        await subscriptions.sync_all_subscription_states()
    # The retry reads the new pending marker and skips the renewed subscription.
    assert session_factory.call_count == 2
    sync.assert_awaited_once()
    async with sessions() as check:
        sub = await check.get(Subscription, sub_id)
        assert sub.volume_bytes == 2000 and sub.used_bytes == 300
        assert sub.panel_sync_pending
        assert await check.scalar(select(Wallet.balance)) == Decimal("100")
        assert await check.scalar(select(func.count()).select_from(WalletTransaction)) == 0


async def test_worker_replays_paid_renewal_after_restart_without_another_debit(sessions):
    from apps.worker.jobs import panel_sync
    from services.renewal import apply_renewal
    from services.wallet.manager import WalletManager

    user_id, sub_id = await seed(sessions)
    async with sessions() as writer:
        sub = await writer.get(Subscription, sub_id)
        await WalletManager(writer).process_transaction(
            user_id=user_id,
            amount=Decimal("10"),
            transaction_type="renewal",
            direction="debit",
            currency="USD",
            reference_type="subscription",
            reference_id=sub_id,
        )
        await apply_renewal(
            session=writer, subscription=sub, renew_type="volume", amount=1,
        )
        await writer.commit()

    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(return_value=NS(xui_limit_ip=0))
    remote = AsyncMock(side_effect=[TimeoutError("offline"), None])
    with (
        patch.object(panel_sync, "AsyncSessionFactory", sessions),
        patch.object(panel_sync, "AppSettingsRepository", return_value=repo),
        patch.object(panel_sync, "_sync_xui_limits", remote),
    ):
        await panel_sync.sync_pending_panel_states()
        async with sessions() as check:
            pending = await check.get(Subscription, sub_id)
            assert pending.panel_sync_pending and pending.panel_sync_attempts == 1
            assert pending.panel_sync_error == "TimeoutError"
        # A fresh worker invocation reads the committed intent from the DB.
        retry_time = datetime.now(UTC) + timedelta(minutes=2)
        with patch.object(panel_sync, "utcnow", return_value=retry_time):
            await panel_sync.sync_pending_panel_states()
        await panel_sync.sync_pending_panel_states()
    assert remote.await_count == 2
    async with sessions() as check:
        delivered = await check.get(Subscription, sub_id)
        assert not delivered.panel_sync_pending and delivered.panel_sync_error is None
        assert delivered.volume_bytes == 1000 + 1024 ** 3 and delivered.used_bytes == 300
        assert await check.scalar(
            select(Wallet.balance).where(Wallet.user_id == user_id)
        ) == Decimal("90")
        assert await check.scalar(select(func.count()).select_from(WalletTransaction)) == 1


async def test_restore_database_renames_and_connection_policy_are_atomic():
    """Only random empty databases in the explicitly disposable PG instance."""
    engine = create_async_engine(os.environ["AUDIT_TEST_DATABASE_URL"])
    target = "upgrade_restore_" + uuid4().hex
    shadow, previous = target + "_shadow", target + "_before"
    try:
        async with engine.connect() as raw:
            admin = await raw.execution_options(isolation_level="AUTOCOMMIT")
            for name in (target, shadow):
                await admin.execute(text(f'CREATE DATABASE "{name}"'))
            async with engine.connect() as swap:
                transaction = await swap.begin()
                await swap.execute(text(f'ALTER DATABASE "{target}" ALLOW_CONNECTIONS false'))
                await swap.execute(text(f'ALTER DATABASE "{target}" RENAME TO "{previous}"'))
                await swap.execute(text(f'ALTER DATABASE "{shadow}" RENAME TO "{target}"'))
                # A cutover failure/abort must restore both names and policy.
                await transaction.rollback()
            rows = dict(
                (
                    await admin.execute(
                        text(
                            "SELECT datname, datallowconn FROM pg_database "
                            "WHERE datname IN (:target, :shadow, :previous)"
                        ),
                        {"target": target, "shadow": shadow, "previous": previous},
                    )
                ).all()
            )
            assert rows == {target: True, shadow: True}
            async with engine.begin() as swap:
                await swap.execute(text(f'ALTER DATABASE "{target}" ALLOW_CONNECTIONS false'))
                await swap.execute(text(f'ALTER DATABASE "{target}" RENAME TO "{previous}"'))
                await swap.execute(text(f'ALTER DATABASE "{shadow}" RENAME TO "{target}"'))
            rows = dict(
                (
                    await admin.execute(
                        text(
                            "SELECT datname, datallowconn FROM pg_database "
                            "WHERE datname IN (:target, :shadow, :previous)"
                        ),
                        {"target": target, "shadow": shadow, "previous": previous},
                    )
                ).all()
            )
            assert rows == {target: True, previous: False}
    finally:
        async with engine.connect() as raw:
            admin = await raw.execution_options(isolation_level="AUTOCOMMIT")
            for name in (target, shadow, previous):
                await admin.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await engine.dispose()
