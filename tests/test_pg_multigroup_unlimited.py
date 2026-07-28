"""PasarGuard multi-group plans + unlimited-GB configs.

Multi-group: Plan.pg_group_ids holds the remote group ids chosen at plan
creation (bot FSM picker / dashboard form); provisioning passes the full
list to create_user_in_bundle → PGUserCreate.group_ids, so the config is a
member of every chosen group and its sub link carries every group's
inbounds. NULL/empty keeps the legacy single group from the plan's inbound.

Unlimited GB: volume_bytes=0 → data_limit omitted at create (panel treats
a missing key as unlimited), the expiry job never volume-caps it, and
quota displays render «نامحدود» via format_plan_volume.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from apps.api.routes.dashboard.plans import (
    PlanCreateBody,
    PlanUpdateBody,
    create_plan,
    list_inbounds_for_picker,
    update_plan,
)
from core.formatting import format_plan_volume
from schemas.internal.pasarguard import PGUserCreate
from services.pasarguard.client import PasarGuardClient


# ─── client: create_user_in_bundle group list ────────────────────────────────


def _client_with_create_stub():
    client = PasarGuardClient.__new__(PasarGuardClient)
    captured: list[PGUserCreate] = []

    async def fake_create(payload):
        captured.append(payload)
        return MagicMock()

    client.create_user = fake_create
    return client, captured


async def test_single_group_legacy_when_no_bundle_ids():
    client, captured = _client_with_create_stub()
    await client.create_user_in_bundle(
        username="u", status="on_hold", expire=None, data_limit=None, bundle_id=5,
    )
    assert captured[0].group_ids == [5]


async def test_bundle_ids_becomes_full_group_list_with_primary_kept():
    client, captured = _client_with_create_stub()
    await client.create_user_in_bundle(
        username="u", status="on_hold", expire=None, data_limit=None,
        bundle_id=5, bundle_ids=[6, 7],
    )
    assert captured[0].group_ids == [5, 6, 7]


async def test_bundle_ids_already_containing_primary_not_duplicated():
    client, captured = _client_with_create_stub()
    await client.create_user_in_bundle(
        username="u", status="on_hold", expire=None, data_limit=None,
        bundle_id=5, bundle_ids=[5, 6],
    )
    assert captured[0].group_ids == [5, 6]


# ─── formatting ──────────────────────────────────────────────────────────────


def test_format_plan_volume_zero_is_unlimited():
    assert format_plan_volume(0) == "نامحدود"
    assert format_plan_volume(None) == "نامحدود"
    assert format_plan_volume(-5) == "نامحدود"


def test_format_plan_volume_positive_matches_bytes_formatter():
    assert format_plan_volume(5 * 1024**3) == "5 GB"
    assert format_plan_volume(int(1.5 * 1024**3)) == "1.50 GB"


# ─── dashboard API: group validation on create/update ────────────────────────


@pytest.fixture
def dashboard_admin():
    admin = MagicMock()
    admin.username = "boss"
    return admin


def _pg_inbound(server_id=None, remote_id=6):
    inbound = MagicMock()
    inbound.id = uuid4()
    inbound.server_id = server_id or uuid4()
    inbound.xui_inbound_remote_id = remote_id
    inbound.protocol = "pasarguard"
    inbound.metadata_ = {"marzban_bundle": True}
    inbound.remark = "G1"
    return inbound


def _groups_execute_result(valid_ids):
    result = MagicMock()
    result.all.return_value = [(gid,) for gid in valid_ids]
    return result


def _create_body(**over):
    base = dict(
        name="پلن تست",
        protocol="pasarguard",
        inbound_id=uuid4(),
        duration_days=30,
        volume_gb=0,  # unlimited GB
        price=5.0,
    )
    base.update(over)
    return PlanCreateBody(**base)


async def test_create_plan_persists_validated_group_list(mock_session, dashboard_admin):
    inbound = _pg_inbound()
    mock_session.get = AsyncMock(return_value=inbound)
    mock_session.execute = AsyncMock(return_value=_groups_execute_result([6, 7, 8]))

    body = _create_body(inbound_id=inbound.id, pg_group_ids=[6, 7, 7])  # dup → deduped
    result = await create_plan(body, (dashboard_admin, mock_session))

    assert result["ok"] is True
    # First add() is the Plan; later ones are audit-log rows.
    plan = mock_session.add.call_args_list[0].args[0]
    assert plan.pg_group_ids == [6, 7]
    assert plan.volume_bytes == 0  # unlimited
    mock_session.commit.assert_awaited_once()


async def test_create_plan_without_groups_stays_legacy_single_group(mock_session, dashboard_admin):
    inbound = _pg_inbound()
    mock_session.get = AsyncMock(return_value=inbound)

    result = await create_plan(_create_body(inbound_id=inbound.id), (dashboard_admin, mock_session))

    assert result["ok"] is True
    # First add() is the Plan; later ones are audit-log rows.
    plan = mock_session.add.call_args_list[0].args[0]
    assert plan.pg_group_ids is None
    mock_session.execute.assert_not_called()  # no group validation query


async def test_create_plan_groups_rejected_for_non_pg_inbound(mock_session, dashboard_admin):
    inbound = _pg_inbound()
    inbound.protocol = "vless"
    inbound.metadata_ = {}
    mock_session.get = AsyncMock(return_value=inbound)

    with pytest.raises(HTTPException) as exc_info:
        await create_plan(
            _create_body(inbound_id=inbound.id, pg_group_ids=[6]),
            (dashboard_admin, mock_session),
        )

    assert exc_info.value.status_code == 400
    mock_session.add.assert_not_called()


async def test_create_plan_unknown_group_rejected(mock_session, dashboard_admin):
    inbound = _pg_inbound()
    mock_session.get = AsyncMock(return_value=inbound)
    mock_session.execute = AsyncMock(return_value=_groups_execute_result([6, 7]))

    with pytest.raises(HTTPException) as exc_info:
        await create_plan(
            _create_body(inbound_id=inbound.id, pg_group_ids=[6, 99]),
            (dashboard_admin, mock_session),
        )

    assert exc_info.value.status_code == 400
    assert "99" in exc_info.value.detail
    mock_session.add.assert_not_called()


async def test_create_plan_groups_require_an_inbound(mock_session, dashboard_admin):
    with pytest.raises(HTTPException) as exc_info:
        await create_plan(
            _create_body(inbound_id=None, pg_group_ids=[6]),
            (dashboard_admin, mock_session),
        )

    assert exc_info.value.status_code == 400
    mock_session.add.assert_not_called()


async def test_update_plan_groups_blocked_when_plan_has_subscriptions(
    mock_session, dashboard_admin, plan_id
):
    plan = MagicMock()
    plan.id = plan_id
    plan.pg_group_ids = None
    plan.inbound_id = uuid4()
    mock_session.scalar = AsyncMock(side_effect=[plan, 3])  # plan, sub_count

    with pytest.raises(HTTPException) as exc_info:
        await update_plan(
            plan_id, PlanUpdateBody(pg_group_ids=[6]), (dashboard_admin, mock_session),
        )

    assert exc_info.value.status_code == 400
    assert "سرویس" in exc_info.value.detail
    mock_session.commit.assert_not_called()


async def test_update_plan_sets_groups_without_subscriptions(
    mock_session, dashboard_admin, plan_id
):
    inbound = _pg_inbound()
    plan = MagicMock()
    plan.id = plan_id
    plan.pg_group_ids = None
    plan.inbound_id = inbound.id
    mock_session.scalar = AsyncMock(side_effect=[plan, 0])
    mock_session.get = AsyncMock(return_value=inbound)
    mock_session.execute = AsyncMock(return_value=_groups_execute_result([6, 7]))

    result = await update_plan(
        plan_id, PlanUpdateBody(pg_group_ids=[6, 7]), (dashboard_admin, mock_session),
    )

    assert result["ok"] is True
    assert plan.pg_group_ids == [6, 7]
    mock_session.commit.assert_awaited_once()


async def test_update_plan_empty_group_list_clears_to_single_group(
    mock_session, dashboard_admin, plan_id
):
    plan = MagicMock()
    plan.id = plan_id
    plan.pg_group_ids = [6, 7]
    plan.inbound_id = uuid4()
    mock_session.scalar = AsyncMock(side_effect=[plan, 0])

    result = await update_plan(
        plan_id, PlanUpdateBody(pg_group_ids=[]), (dashboard_admin, mock_session),
    )

    assert result["ok"] is True
    assert plan.pg_group_ids is None
    mock_session.commit.assert_awaited_once()


# ─── dashboard picker feed ───────────────────────────────────────────────────


async def test_inbounds_picker_marks_pasarguard_groups(mock_session, dashboard_admin):
    pg = _pg_inbound(remote_id=6)
    pg.server = MagicMock(is_active=True)
    pg.server.name = "pg-server"
    pg.port = None
    xui = MagicMock()
    xui.id = uuid4()
    xui.server_id = uuid4()
    xui.xui_inbound_remote_id = 3
    xui.protocol = "vless"
    xui.metadata_ = {}
    xui.remark = "vless-in"
    xui.port = 443
    xui.server = MagicMock(is_active=True)
    xui.server.name = "xui-server"

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [pg, xui]
    mock_session.execute = AsyncMock(return_value=result_mock)

    result = await list_inbounds_for_picker((dashboard_admin, mock_session))

    items = {item["remote_id"]: item for item in result["items"]}
    assert items[6]["is_pasarguard_group"] is True
    assert items[3]["is_pasarguard_group"] is False
    assert items[6]["server_id"] == str(pg.server_id)
