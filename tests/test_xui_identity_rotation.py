"""A successful response must not advance local links without panel readback."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from schemas.internal.xui import XUIClient
from services.xui.client import (
    SanaeiXUIClient,
    XUIClientConfig,
    XUIIdentityVerificationError,
    XUIRequestError,
)

OLD = {"id": "old", "email": "fixture", "subId": "old-sub", "flow": "keep", "reset": 30}
NEW = {**OLD, "id": "new", "subId": "new-sub"}


def _inbound(clients, identifier=1):
    return {"id": identifier, "protocol": "vless", "settings": {"clients": clients}}


@pytest.mark.parametrize(
    ("readback", "confirmed"),
    [
        ([_inbound([NEW])], True),
        ([_inbound([OLD])], False),
        ([_inbound([NEW, OLD])], False),
        ([_inbound([{**NEW, "subId": "wrong"}])], False),
        ([_inbound([{**NEW, "email": "other"}])], False),
        ([_inbound([NEW, NEW])], False),
        ([_inbound([NEW], identifier=2)], False),
        ([_inbound(None)], False),
        ([], False),
    ],
)
async def test_rotation_checks_new_id_token_email_inbound_and_removal_of_old_id(
    readback, confirmed,
):
    reads = 0
    payloads = []

    def respond(request):
        nonlocal reads
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/panel/api/inbounds/list":
            reads += 1
            return httpx.Response(
                200, json={"success": True, "obj": [_inbound([OLD])] if reads == 1 else readback}
            )
        assert request.url.path == "/panel/api/inbounds/updateClient/old"
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )
        kwargs = dict(
            inbound_id=1, client_id="old",
            client=XUIClient(id="new", email="fixture", subId="new-sub"),
        )
        if confirmed:
            await client.patch_client(**kwargs)
        else:
            with pytest.raises(XUIIdentityVerificationError):
                await client.patch_client(**kwargs)
    assert reads == 2 and len(payloads) == 1
    sent = json.loads(payloads[0]["settings"])["clients"][0]
    assert sent["id"] == "new" and sent["subId"] == "new-sub"
    assert sent["flow"] == "keep" and sent["reset"] == 30


async def test_rotation_timeout_does_not_blindly_replay_consumed_old_target():
    writes = []

    def respond(request):
        if request.url.path == "/login":
            return httpx.Response(200, json={"success": True})
        writes.append(request)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )
        with pytest.raises(XUIRequestError):
            await client.update_client(
                inbound_id=1, client_id="old",
                client=XUIClient(id="new", email="fixture", subId="new-sub"),
            )
    assert len(writes) == 1


async def test_noop_rotation_does_not_persist_new_local_credentials(mock_session):
    from apps.bot.handlers.user import my_configs

    record = NS(
        panel_kind=None, inbound=NS(
            xui_inbound_remote_id=1,
            server=NS(health_status="healthy", is_active=True, credentials=NS()),
        ),
        client_uuid="old", xui_client_remote_id="old", email="fixture",
        username="fixture", is_active=True, sub_link="https://sub.example/old-sub",
    )
    sub = NS(
        id=uuid4(), status="active", xui_client=record, ends_at=None,
        volume_bytes=100, sub_link=record.sub_link,
    )
    mock_session.scalar.return_value = sub
    callback = NS(answer=AsyncMock(), from_user=NS(id=123))
    callback_data = NS(subscription_id=sub.id)

    def respond(request):
        if request.url.path == "/panel/api/inbounds/list":
            return httpx.Response(200, json={"success": True, "obj": [_inbound([OLD])]})
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(
        base_url="https://panel.example/", transport=httpx.MockTransport(respond)
    ) as http:
        client = SanaeiXUIClient(
            XUIClientConfig("https://panel.example", "fixture", SecretStr("fixture")),
            http_client=http,
        )

        @asynccontextmanager
        async def panel(server):
            yield client

        with (
            patch.object(my_configs.UserRepository, "get_by_telegram_id", AsyncMock(
                return_value=NS(id=uuid4())
            )),
            patch.object(
                my_configs.AppSettingsRepository, "get_service_security_settings",
                AsyncMock(return_value=NS(xui_limit_ip=1)),
            ),
            patch("services.xui.runtime.create_xui_client_for_server", panel),
            patch.object(my_configs, "safe_edit_or_send", AsyncMock()) as show,
        ):
            await my_configs.reset_uuid_handler(callback, callback_data, mock_session)
    assert record.client_uuid == record.xui_client_remote_id == "old"
    assert record.sub_link == sub.sub_link == "https://sub.example/old-sub"
    mock_session.flush.assert_not_awaited()
    assert "✅" not in show.await_args.args[1]
