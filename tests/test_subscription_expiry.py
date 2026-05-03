from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.database import utcnow
from apps.worker.jobs.subscriptions import _expire_subscription_in_xui, _get_expiry_reason


class FakeXUIClient:
    def __init__(self):
        self.calls = []

    async def update_client(self, *, inbound_id, client_id, client):
        self.calls.append(
            {
                "inbound_id": inbound_id,
                "client_id": client_id,
                "client": client,
            }
        )


def make_subscription(*, used_bytes=1024, volume_bytes=1024):
    inbound = SimpleNamespace(xui_inbound_remote_id=77)
    xui_record = SimpleNamespace(
        xui_client_remote_id="remote-client-id",
        client_uuid="old-uuid",
        email="user-1",
        inbound=inbound,
        is_active=True,
        sub_link="https://example.com/sub/existing-sub-id",
    )
    return SimpleNamespace(
        id=uuid4(),
        xui_client=xui_record,
        sub_link=None,
        volume_bytes=volume_bytes,
        used_bytes=used_bytes,
        ends_at=utcnow() + timedelta(days=5),
        status="active",
        expired_at=None,
    )


def test_get_expiry_reason_for_volume_limit():
    subscription = make_subscription(used_bytes=2048, volume_bytes=1024)

    assert _get_expiry_reason(subscription, utcnow()) == "volume"


@pytest.mark.asyncio
async def test_expire_subscription_rotates_uuid_and_disables_client():
    subscription = make_subscription()
    xui_client = FakeXUIClient()
    now = utcnow()

    expired = await _expire_subscription_in_xui(
        xui_client,
        subscription,
        now=now,
        reason="volume",
    )

    assert expired is True
    assert subscription.status == "expired"
    assert subscription.expired_at == now
    assert subscription.xui_client.is_active is False
    assert subscription.xui_client.client_uuid != "old-uuid"
    assert len(xui_client.calls) == 1
    call = xui_client.calls[0]
    assert call["inbound_id"] == 77
    assert call["client_id"] == "remote-client-id"
    assert call["client"].enable is False
    assert call["client"].uuid == subscription.xui_client.client_uuid
    assert call["client"].sub_id == "existing-sub-id"
