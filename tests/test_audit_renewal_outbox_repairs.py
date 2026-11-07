"""Regressions for audit findings 4-6; external panel IO stays in memory."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine

from schemas.internal.pasarguard import PGUserModify
from services import renewal


@pytest.mark.asyncio
async def test_renewal_rejects_owner_changed_before_row_lock(mock_session):
    owner, recipient = uuid4(), uuid4()
    ends_at = datetime.now(UTC) + timedelta(days=5)
    sub = NS(
        id=uuid4(),
        user_id=owner,
        status="active",
        ends_at=ends_at,
        volume_bytes=1000,
        used_bytes=0,
        plan_id=None,
        panel_sync_pending=False,
    )

    async def refresh(obj, *, attribute_names):
        if "user_id" in attribute_names:
            obj.user_id = recipient

    mock_session.refresh.side_effect = refresh
    with pytest.raises(renewal.RenewalNotAllowedError):
        await renewal.apply_renewal(
            session=mock_session,
            subscription=sub,
            renew_type="time",
            amount=30,
            expected_user_id=owner,
        )
    assert sub.user_id == recipient and sub.ends_at == ends_at
    assert not sub.panel_sync_pending
    mock_session.commit.assert_not_awaited()
    mock_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_invoice_paid_after_transfer_keeps_funds_in_payers_wallet(mock_session):
    from services import payment as service

    owner, recipient, sub_id = uuid4(), uuid4(), uuid4()
    payment = NS(
        id=uuid4(),
        user_id=owner,
        provider="nowpayments",
        provider_payment_id="123",
        kind="direct_renewal",
        actually_paid=None,
        price_currency="USD",
        price_amount=Decimal("7"),
        payment_status="waiting",
        callback_payload={"sub_id": str(sub_id), "renew_type": "time", "renew_amount": 30},
    )
    sub = NS(id=sub_id, user_id=recipient, status="active", plan_id=None)
    mock_session.scalar.side_effect = [sub, NS(telegram_id=123)]
    balance, entries = Decimal("0"), []

    async def transact(**kwargs):
        nonlocal balance
        entries.append(kwargs["direction"])
        balance += kwargs["amount"] * (1 if kwargs["direction"] == "credit" else -1)

    wallet = NS(process_transaction=transact)
    bot = NS(send_message=AsyncMock(), session=NS(close=AsyncMock()))

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield True

    with (
        patch.object(service, "WalletManager", return_value=wallet),
        patch.object(service, "_get_shared_bot", return_value=bot),
        patch("core.redis.distributed_lock", lock),
        patch("services.provisioning.manager.ProvisioningManager") as manager,
        patch.object(renewal, "apply_renewal", AsyncMock()),
        patch("services.sales_notifications.notify_renewal", AsyncMock()),
    ):
        manager.return_value.preflight_check_subscription = AsyncMock(return_value=(True, None))
        assert not await service.process_successful_payment(mock_session, payment, Decimal("7"))
    assert balance == Decimal("7") and entries == ["credit"]
    assert payment.actually_paid == Decimal("7")
    assert payment.callback_payload["renewal_refused"] is True
    assert not payment.callback_payload.get("renewal_applied")


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["disabled", "refunded", "cancelled", "unknown"])
async def test_renewal_rejects_ineligible_status_refreshed_under_lock(mock_session, new_status):
    ends_at = datetime.now(UTC) + timedelta(days=5)
    sub = NS(
        id=uuid4(),
        status="active",
        ends_at=ends_at,
        volume_bytes=1000,
        used_bytes=0,
        plan_id=None,
        panel_sync_pending=False,
    )

    async def refresh(obj, **kwargs):
        obj.status = new_status

    mock_session.refresh.side_effect = refresh
    mock_session.no_autoflush = MagicMock()
    mock_session.scalar.return_value = NS(
        inbound=NS(server=NS(credentials=NS())),
        xui_client_remote_id="fixture",
        client_uuid="fixture",
    )
    with patch.object(renewal, "record_is_marzban_family", return_value=False):
        with pytest.raises(renewal.RenewalNotAllowedError):
            await renewal.apply_renewal(
                session=mock_session, subscription=sub, renew_type="time", amount=30
            )
    assert sub.ends_at == ends_at and sub.volume_bytes == 1000
    assert not sub.panel_sync_pending
    # The caller owns the wallet transaction and must receive the exception.
    mock_session.commit.assert_not_awaited()
    mock_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["pasarguard", "rebecca"])
@pytest.mark.parametrize(
    "status,expected",
    [
        ("active", {"status": "active", "expire": None, "data_limit": None}),
        ("pending_activation", {"data_limit": None}),
    ],
)
async def test_unlimited_renewal_sends_explicit_null_limits_over_http(family, status, expected):
    from services.pasarguard.client import PasarGuardClient, PasarGuardClientConfig
    from services.rebecca.client import RebeccaClient, RebeccaClientConfig

    remote = {"username": "fixture", "expire": 2000000000, "data_limit": 1000}
    writes = []

    def respond(request):
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "fixture", "token_type": "bearer"})
        assert request.method == "PUT" and request.url.path == "/api/user/fixture"
        writes.append(json.loads(request.content))
        remote.update(writes[-1])
        return httpx.Response(200, json=remote)

    client_cls, config_cls = (
        (PasarGuardClient, PasarGuardClientConfig)
        if family == "pasarguard"
        else (RebeccaClient, RebeccaClientConfig)
    )
    async with httpx.AsyncClient(
        base_url="https://panel.invalid/", transport=httpx.MockTransport(respond)
    ) as http:
        client = client_cls(
            config_cls("https://panel.invalid", "fixture", SecretStr("fixture")), http_client=http
        )

        @asynccontextmanager
        async def factory(server):
            yield client

        sub = NS(status=status, ends_at=None, volume_bytes=0)
        record = NS(inbound=NS(server=NS()), panel_username="fixture", username="fixture")
        with patch.object(renewal, "marzban_client_for_server", factory):
            await renewal._sync_pasarguard_limits(sub, record)
    assert writes == [expected]
    assert remote["data_limit"] is None
    assert remote["expire"] == (None if status == "active" else 2000000000)


def test_modify_omitted_fields_remain_unchanged():
    assert PGUserModify().to_payload() == {}
    assert PGUserModify(status="disabled").to_payload() == {"status": "disabled"}


@pytest.mark.asyncio
async def test_outbox_filters_backoff_before_batch_limit(mock_session):
    from apps.worker.jobs import panel_sync as job

    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    # Execute the production SELECT on a small SQL database: no simulated WHERE.
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE subscriptions (id CHAR(32) PRIMARY KEY, "
            "panel_sync_pending BOOLEAN, panel_sync_attempts BIGINT, "
            "panel_sync_attempted_at DATETIME)"
        )
        sleeping = [
            (uuid4().hex, True, 7, (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S.%f"))
            for _ in range(50)
        ]
        due_id = uuid4()
        connection.exec_driver_sql(
            "INSERT INTO subscriptions VALUES (?, ?, ?, ?)",
            [
                *sleeping,
                (
                    due_id.hex,
                    True,
                    1,
                    (now - timedelta(seconds=70)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                ),
            ],
        )
        selected = []

        async def scalars(statement):
            result = connection.execute(statement).scalars().all()
            selected.extend(result)
            return NS(all=lambda: result)

        mock_session.scalars.side_effect = scalars
        mock_session.scalar.return_value = None

        @asynccontextmanager
        async def factory():
            yield mock_session

        with (
            patch.object(job, "AsyncSessionFactory", factory),
            patch.object(job, "utcnow", return_value=now),
        ):
            await job.sync_pending_panel_states()
    engine.dispose()
    assert selected == [due_id]


@pytest.mark.asyncio
async def test_outbox_sql_and_delivery_share_retry_deadline_boundaries(mock_session):
    from apps.worker.jobs import panel_sync as job

    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    rows, expected = [], set()
    for attempts, delay in [
        (0, 30),
        (1, 60),
        (2, 120),
        (3, 240),
        (4, 480),
        (5, 960),
        (6, 1920),
        (7, 3600),
        (100, 3600),
    ]:
        for offset in [-1, 0, 1]:
            row = NS(
                id=uuid4(),
                panel_sync_pending=True,
                panel_sync_attempts=attempts,
                panel_sync_attempted_at=now - timedelta(seconds=delay + offset),
                xui_client=None,
            )
            rows.append(row)
            if offset >= 0:
                expected.add(row.id)
    never_attempted = NS(
        id=uuid4(),
        panel_sync_pending=True,
        panel_sync_attempts=0,
        panel_sync_attempted_at=None,
        xui_client=None,
    )
    rows.append(never_attempted)
    expected.add(never_attempted.id)

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE subscriptions (id CHAR(32) PRIMARY KEY, "
            "panel_sync_pending BOOLEAN, panel_sync_attempts BIGINT, "
            "panel_sync_attempted_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO subscriptions VALUES (?, ?, ?, ?)",
            [
                (
                    r.id.hex,
                    r.panel_sync_pending,
                    r.panel_sync_attempts,
                    r.panel_sync_attempted_at.strftime("%Y-%m-%d %H:%M:%S.%f")
                    if r.panel_sync_attempted_at
                    else None,
                )
                for r in rows
            ],
        )
        selected = []

        async def scalars(statement):
            result = connection.execute(statement).scalars().all()
            selected.extend(result)
            return NS(all=lambda: result)

        mock_session.scalars.side_effect = scalars
        mock_session.scalar.return_value = None

        @asynccontextmanager
        async def factory():
            yield mock_session

        with (
            patch.object(job, "AsyncSessionFactory", factory),
            patch.object(job, "utcnow", return_value=now),
        ):
            await job.sync_pending_panel_states()
            for row in rows:
                previous_attempts = row.panel_sync_attempts
                await job.deliver_panel_state(mock_session, row)
                assert row.panel_sync_attempts == previous_attempts + int(row.id in expected)
    engine.dispose()
    assert set(selected) == expected
