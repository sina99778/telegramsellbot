"""Regression tests for medium-severity provisioning fixes (services/provisioning/manager.py).

Covers:
- #64: _disable_xui_client sent an X-UI update payload WITHOUT subId. X-UI's
  updateClient replaces the whole client object, so "subId": "" permanently
  wiped the panel-side subscription id — the user's saved sub link died even
  after an unban/re-enable. Fixed: the existing subId is extracted from the
  stored sub link (same convention as the worker sync paths) and re-sent.
- #65: migrating an EXPIRED imported sub carried the past ends_at onto the
  new panel client (born expired, refuses traffic) while flipping the DB
  status to "active". Fixed: the flip only happens when the carried-over
  expiry is unset or still in the future; date-expired imports stay
  "expired" so a time renewal revives both DB and panel consistently.
- #66: provision_subscription wrote username=config_name into the UNIQUE
  xui_clients.username column with no provisioning-time re-check. A deferred
  payment (card-to-card approval, gateway IPN) confirming hours after the
  name-entry check crashed on IntegrityError AFTER the user paid. Fixed:
  a suffixed retry loop mirroring the imported-migration pattern — the
  user-visible remark never changes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.provisioning.manager import ProvisioningManager


def _fake_panel_cm(fake_api):
    @asynccontextmanager
    async def _cm(server):
        yield fake_api

    return _cm


# ─── #64: _disable_xui_client preserves the panel client's subId ─────────────


def _make_disable_fixture(mock_session, *, record_sub_link):
    manager = ProvisioningManager(mock_session)
    fake_api = MagicMock()
    fake_api.update_client = AsyncMock()
    manager._get_xui_client_for_server = _fake_panel_cm(fake_api)

    xui_record = MagicMock()
    xui_record.panel_kind = None  # plain X-UI record
    xui_record.inbound = MagicMock()
    xui_record.inbound.xui_inbound_remote_id = 7
    xui_record.xui_client_remote_id = "remote-id"
    xui_record.client_uuid = "client-uuid"
    xui_record.email = "cfg_abc123"
    xui_record.subscription_id = uuid4()
    xui_record.sub_link = record_sub_link
    return manager, fake_api, xui_record


@pytest.mark.asyncio
async def test_disable_xui_client_preserves_sub_id_from_subscription_link(mock_session):
    manager, fake_api, xui_record = _make_disable_fixture(
        mock_session, record_sub_link="https://host:2096/sub/recordsubid"
    )

    with patch("services.provisioning.manager.AppSettingsRepository") as repo_cls:
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=2)
        )
        await manager._disable_xui_client(
            xui_record=xui_record,
            volume_bytes=10 * 1024**3,
            ends_at=None,
            sub_link="https://host:2096/sub/subfromsub",
        )

    fake_api.update_client.assert_awaited_once()
    client = fake_api.update_client.await_args.kwargs["client"]
    assert client.enable is False
    # The subscription's own link wins (worker convention), and the
    # serialized payload must NOT send an empty subId.
    assert client.sub_id == "subfromsub"
    assert client.to_xui_payload()["subId"] == "subfromsub"


@pytest.mark.asyncio
async def test_disable_xui_client_falls_back_to_record_sub_link(mock_session):
    manager, fake_api, xui_record = _make_disable_fixture(
        mock_session, record_sub_link="https://host:2096/sub/recordsubid"
    )

    with patch("services.provisioning.manager.AppSettingsRepository") as repo_cls:
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=2)
        )
        await manager._disable_xui_client(
            xui_record=xui_record,
            volume_bytes=10 * 1024**3,
            ends_at=None,
        )

    client = fake_api.update_client.await_args.kwargs["client"]
    assert client.to_xui_payload()["subId"] == "recordsubid"


# ─── #65: expired imported sub migration keeps a consistent DB status ────────


def _make_migrate_fixture(mock_session, *, status, ends_at):
    sub = MagicMock()
    sub.id = uuid4()
    sub.source = "imported_legacy"
    sub.legacy_remark = "oldname"
    sub.legacy_link = None
    sub.plan_id = None
    sub.plan = None
    sub.status = status
    sub.ends_at = ends_at
    sub.expired_at = ends_at
    sub.volume_bytes = 10 * 1024**3
    sub.lifetime_used_bytes = 0
    sub.sub_link = "https://old/sub/x"

    target_inbound = MagicMock()
    target_inbound.id = uuid4()
    target_inbound.is_active = True
    target_inbound.remark = "inb1"
    target_inbound.xui_inbound_remote_id = 7
    target_inbound.server.is_active = True

    # scalar order: sub → target_inbound → existing_xui (None)
    mock_session.scalar = AsyncMock(side_effect=[sub, target_inbound, None])

    manager = ProvisioningManager(mock_session)
    manager._recover_from_sublink_header = AsyncMock(return_value=(None, None))
    manager._read_legacy_client_usage = AsyncMock(return_value=0)
    manager._delete_stale_panel_clients = AsyncMock(return_value=0)
    fake_api = MagicMock()
    fake_api.add_client_to_inbound = AsyncMock()
    manager._get_xui_client_for_server = _fake_panel_cm(fake_api)
    return manager, sub, target_inbound, fake_api


def _migrate_patches():
    repo_patch = patch("repositories.settings.AppSettingsRepository")
    link_patch = patch(
        "services.provisioning.manager.build_sub_link",
        return_value="https://new/sub/y",
    )
    uri_patch = patch(
        "services.provisioning.manager.build_vless_uri",
        return_value="vless://new",
    )
    return repo_patch, link_patch, uri_patch


@pytest.mark.asyncio
async def test_migrate_expired_import_with_past_expiry_stays_expired(mock_session):
    past = datetime.now(timezone.utc) - timedelta(days=30)
    manager, sub, target_inbound, fake_api = _make_migrate_fixture(
        mock_session, status="expired", ends_at=past
    )

    repo_patch, link_patch, uri_patch = _migrate_patches()
    with repo_patch as repo_cls, link_patch, uri_patch:
        repo_cls.return_value.get_migration_target_inbound_ids = AsyncMock(return_value=[])
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=1)
        )
        result = await manager.migrate_imported_subscription_to_inbound(
            subscription_id=sub.id,
            target_inbound_id=target_inbound.id,
        )

    # The panel client IS re-provisioned (operator's re-provision intent)…
    fake_api.add_client_to_inbound.assert_awaited_once()
    # …but the DB must NOT claim "active" for a born-expired client.
    assert sub.status == "expired"
    assert sub.expired_at == past
    assert result.subscription is sub


@pytest.mark.asyncio
async def test_migrate_expired_import_without_end_date_flips_to_active(mock_session):
    """Companion: when no expiry carries over (ends_at=None → expiryTime=0,
    i.e. usable client), the historical flip back to active is preserved."""
    manager, sub, target_inbound, _ = _make_migrate_fixture(
        mock_session, status="expired", ends_at=None
    )

    repo_patch, link_patch, uri_patch = _migrate_patches()
    with repo_patch as repo_cls, link_patch, uri_patch:
        repo_cls.return_value.get_migration_target_inbound_ids = AsyncMock(return_value=[])
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=1)
        )
        await manager.migrate_imported_subscription_to_inbound(
            subscription_id=sub.id,
            target_inbound_id=target_inbound.id,
        )

    assert sub.status == "active"
    assert sub.expired_at is None


# ─── #66: provisioning-time retry on UNIQUE username/email collisions ────────


def _make_provision_fixture(mock_session):
    plan = MagicMock()
    plan.id = uuid4()
    plan.is_active = True
    plan.volume_bytes = 5 * 1024**3
    plan.effective_ip_limit = MagicMock(return_value=2)

    inbound = MagicMock()
    inbound.id = uuid4()
    inbound.is_active = True
    inbound.xui_inbound_remote_id = 5
    inbound.protocol = "vless"
    server = inbound.server
    server.is_active = True
    server.max_clients = None
    server.panel_type = "xui"  # NOT Marzban-family → native X-UI path
    plan.inbound = inbound

    order = MagicMock()
    order.user_id = uuid4()
    order.status = "paid"

    # scalar order: plan → ready_pool (None)
    mock_session.scalar = AsyncMock(side_effect=[plan, None])
    mock_session.get = AsyncMock(return_value=order)

    manager = ProvisioningManager(mock_session)
    manager._generate_unique_client_identity = AsyncMock(
        return_value=("client-uuid", "u_throwaway", "u_throwaway@tg.local", "abcdef9999999999")
    )
    fake_api = MagicMock()
    fake_api.add_client_to_inbound = AsyncMock()
    fake_api.delete_client = AsyncMock()
    manager._get_xui_client_for_server = _fake_panel_cm(fake_api)
    return manager, plan, order, fake_api


def _provision_patches():
    return (
        patch("services.provisioning.manager.AppSettingsRepository"),
        patch("services.provisioning.manager.build_sub_link", return_value="https://h/sub/abcdef"),
        patch("services.provisioning.manager.build_vless_uri", return_value="vless://x") ,
        patch("services.provisioning.manager.reserve_plan_sale", new=AsyncMock(return_value=True)),
        patch("services.provisioning.manager.release_plan_sale", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_provision_retries_with_suffix_on_username_collision(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)
    # Attempt 0: flush#1 (subscription) ok, flush#2 (order/xui rows) raises the
    # DB unique violation a deferred payment hits when the name was claimed
    # in the meantime. Attempt 1 must succeed with a suffixed identity.
    mock_session.flush = AsyncMock(
        side_effect=[
            None,
            Exception('duplicate key value violates unique constraint "uq_xui_clients_username"'),
            None,
            None,
        ]
    )

    repo_patch, link_patch, uri_patch, reserve_patch, release_patch = _provision_patches()
    with repo_patch as repo_cls, link_patch, uri_patch as uri_mock, reserve_patch, release_patch:
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=1)
        )
        result = await manager.provision_subscription(
            user_id=order.user_id,
            plan_id=plan.id,
            order_id=uuid4(),
            config_name="myname",
        )

    # Internal identity got a hex suffix — and is what the panel received.
    assert result.xui_client.username != "myname"
    assert result.xui_client.username.startswith("myname_")
    fake_api.add_client_to_inbound.assert_awaited_once()
    sent_client = fake_api.add_client_to_inbound.await_args.args[1]
    assert sent_client.email == result.xui_client.email
    assert sent_client.email.startswith("myname_")
    # The USER-VISIBLE remark (vless #fragment) is never renamed.
    assert uri_mock.call_args.kwargs["remark"] == "myname"


@pytest.mark.asyncio
async def test_provision_first_attempt_keeps_verbatim_name(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)

    repo_patch, link_patch, uri_patch, reserve_patch, release_patch = _provision_patches()
    with repo_patch as repo_cls, link_patch, uri_patch, reserve_patch, release_patch:
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=1)
        )
        result = await manager.provision_subscription(
            user_id=order.user_id,
            plan_id=plan.id,
            order_id=uuid4(),
            config_name="myname",
        )

    assert result.xui_client.username == "myname"
    assert result.xui_client.email == "myname_abcdef"
    fake_api.add_client_to_inbound.assert_awaited_once()
