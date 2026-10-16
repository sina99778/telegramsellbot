"""Only verified panel absence may complete a queued disable without a write."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from apps.worker.jobs.panel_sync import deliver_panel_state
from schemas.internal.xui import XUIClient
from services.provisioning.manager import ProvisioningManager
from services.xui.client import SanaeiXUIClient, XUIClientAbsentError, XUIClientConfig


def _inbound(identifier=1, clients=None, **overrides):
    return {
        "id": identifier,
        "protocol": "vless",
        "settings": {"clients": [] if clients is None else clients},
        **overrides,
    }


def _subscription():
    server = NS(credentials=NS())
    record = NS(
        panel_kind=None,
        inbound=NS(xui_inbound_remote_id=1, server=server),
        xui_client_remote_id="fixture-id",
        client_uuid="fixture-id",
        email="fixture-email",
        subscription_id=uuid4(),
        sub_link="https://sub.example/fixture",
        is_active=True,
    )
    return NS(
        id=record.subscription_id,
        status="disabled",
        xui_client=record,
        volume_bytes=100,
        ends_at=None,
        sub_link=record.sub_link,
        panel_sync_attempted_at=None,
        panel_sync_attempts=0,
        panel_sync_pending=True,
        panel_sync_error=None,
    )


@pytest.mark.parametrize(
    ("inventory", "delivered", "write_expected"),
    [
        ([_inbound()], True, False),
        ([_inbound(), _inbound(2)], True, False),
        ([_inbound(clients=[{"id": "fixture-id", "email": "fixture-email"}])], True, True),
        ([], False, False),
        ([_inbound(2)], False, False),
        ([_inbound(protocol="trojan")], False, False),
        ([_inbound(settings=None)], False, False),
        ([_inbound(settings={})], False, False),
        ([_inbound(settings={"clients": None})], False, False),
        ([_inbound(), _inbound(2, clients=[None])], False, False),
        ([_inbound(), _inbound(2, clients=[{"email": "other"}])], False, False),
        ([_inbound(), _inbound(2, clients=[{"id": "other"}])], False, False),
        (
            [_inbound(), _inbound(2, clients=[{"id": "fixture-id", "email": "moved"}])],
            False,
            False,
        ),
        (
            [_inbound(), _inbound(2, clients=[{"id": "other", "email": "fixture-email"}])],
            False,
            False,
        ),
    ],
)
async def test_disable_outbox_checks_real_inventory_before_completing(
    mock_session, inventory, delivered, write_expected
):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/panel/api/inbounds/list":
            return httpx.Response(200, json={"success": True, "obj": inventory})
        assert request.url.path == "/panel/api/inbounds/updateClient/fixture-id"
        return httpx.Response(200, json={"success": True})

    sub = _subscription()
    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )
        manager = ProvisioningManager(mock_session, xui_client=client)
        with (
            patch("apps.worker.jobs.panel_sync.ProvisioningManager", return_value=manager),
            patch(
                "services.provisioning.manager.AppSettingsRepository.get_service_security_settings",
                AsyncMock(return_value=NS(xui_limit_ip=1)),
            ),
        ):
            assert await deliver_panel_state(mock_session, sub) is delivered

    assert sub.panel_sync_pending is not delivered
    assert sub.xui_client.is_active is not delivered
    assert sub.panel_sync_attempts == 1
    assert (sub.panel_sync_error is None) is delivered
    writes = [r for r in requests if "/updateClient/" in r.url.path]
    assert bool(writes) is write_expected
    assert len(writes) <= 1


@pytest.mark.parametrize(
    "response",
    [
        {"success": True},
        {"obj": []},
        {"success": False, "obj": []},
        {"success": True, "obj": None},
    ],
)
async def test_incomplete_inventory_never_confirms_disable(mock_session, response):
    def respond(request):
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json=response)

    sub = _subscription()
    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )
        manager = ProvisioningManager(mock_session, xui_client=client)
        with (
            patch("apps.worker.jobs.panel_sync.ProvisioningManager", return_value=manager),
            patch(
                "services.provisioning.manager.AppSettingsRepository.get_service_security_settings",
                AsyncMock(return_value=NS(xui_limit_ip=1)),
            ),
        ):
            assert not await deliver_panel_state(mock_session, sub)
    assert sub.panel_sync_pending and sub.xui_client.is_active


async def test_missing_client_is_still_an_error_for_an_enable_or_renewal():
    def respond(request):
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True})
        assert request.method == "GET"
        return httpx.Response(200, json={"success": True, "obj": [_inbound()]})

    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )
        with pytest.raises(XUIClientAbsentError):
            await client.patch_client(
                inbound_id=1,
                client_id="fixture-id",
                client=XUIClient(id="fixture-id", email="fixture-email", enable=True, totalGB=200),
            )
