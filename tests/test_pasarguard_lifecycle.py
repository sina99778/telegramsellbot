"""Tests for PasarGuard lifecycle dispatch: renewal limit-sync + usage-sync
state transitions, and the username generator. Mock-based (no panel, no DB)."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from schemas.internal.pasarguard import PGUserResponse


class FakePGClient:
    def __init__(self, user=None):
        self._user = user
        self.modify_calls: list = []
        self.get_calls: list = []
        self.deleted: list = []

    async def modify_user(self, username, payload):
        self.modify_calls.append((username, payload.to_payload()))
        return self._user

    async def get_user(self, username):
        self.get_calls.append(username)
        return self._user

    async def delete_user(self, username):
        self.deleted.append(username)

    async def revoke_sub(self, username):
        return self._user


def _fake_cm(client):
    @asynccontextmanager
    async def _cm(server):
        yield client
    return _cm


# ─── renewal limit sync ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_renewal_active_pushes_status_expire_and_data_limit(monkeypatch):
    import services.renewal as rmod

    fake = FakePGClient()
    monkeypatch.setattr(rmod, "create_pasarguard_client_for_server", _fake_cm(fake))

    ends = datetime.now(timezone.utc) + timedelta(days=10)
    sub = NS(status="active", ends_at=ends, volume_bytes=10 * 1024**3, id=uuid4())
    xui_full = NS(
        inbound=NS(server=NS(base_url="http://h:8000")),
        panel_username="u_abc123",
        username="u_abc123",
        is_active=False,
    )

    await rmod._sync_pasarguard_limits(sub, xui_full)

    assert len(fake.modify_calls) == 1
    uname, payload = fake.modify_calls[0]
    assert uname == "u_abc123"
    assert payload["status"] == "active"
    assert payload["expire"] == int(ends.timestamp())  # seconds, not ms
    assert payload["data_limit"] == 10 * 1024**3
    assert xui_full.is_active is True


@pytest.mark.asyncio
async def test_renewal_pending_only_bumps_data_limit(monkeypatch):
    import services.renewal as rmod

    fake = FakePGClient()
    monkeypatch.setattr(rmod, "create_pasarguard_client_for_server", _fake_cm(fake))

    sub = NS(status="pending_activation", ends_at=None, volume_bytes=5 * 1024**3, id=uuid4())
    xui_full = NS(
        inbound=NS(server=NS(base_url="http://h")),
        panel_username="u_x",
        username="u_x",
        is_active=True,
    )

    await rmod._sync_pasarguard_limits(sub, xui_full)

    _uname, payload = fake.modify_calls[0]
    # on_hold sub: keep the first-use timer — only the quota changes.
    assert payload == {"data_limit": 5 * 1024**3}


# ─── usage sync transitions ───────────────────────────────────────────────────


def _sub(**over):
    base = dict(
        status="active",
        xui_client=NS(panel_username="u1", username="u1", usage_bytes=0, is_active=True),
        plan=NS(duration_days=30),
        used_bytes=0,
        usage_sync_failures=0,
        activated_at=None,
        starts_at=None,
        ends_at=None,
        expired_at=None,
        id=uuid4(),
    )
    base.update(over)
    return NS(**base)


@pytest.mark.asyncio
async def test_usage_sync_activates_pending_when_panel_active(monkeypatch, mock_session):
    import apps.worker.jobs.subscriptions as sj

    exp = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    pg_user = PGUserResponse(id=1, username="u1", status="active", used_traffic=12345, expire=exp, subscription_url="/sub/x")
    monkeypatch.setattr(sj, "create_pasarguard_client_for_server", _fake_cm(FakePGClient(pg_user)))

    sub = _sub(status="pending_activation", xui_client=NS(panel_username="u1", username="u1", usage_bytes=0, is_active=False))
    await sj.sync_pasarguard_usage_and_status(mock_session, NS(base_url="http://h"), [sub])

    assert sub.status == "active"
    assert sub.used_bytes == 12345
    assert sub.xui_client.usage_bytes == 12345
    assert sub.xui_client.is_active is True
    assert sub.ends_at is not None and abs(int(sub.ends_at.timestamp()) - exp) <= 1  # uses PG's expire


@pytest.mark.asyncio
async def test_usage_sync_marks_expired_when_panel_limited(monkeypatch, mock_session):
    import apps.worker.jobs.subscriptions as sj

    pg_user = PGUserResponse(id=1, username="u1", status="limited", used_traffic=999, subscription_url="")
    monkeypatch.setattr(sj, "create_pasarguard_client_for_server", _fake_cm(FakePGClient(pg_user)))

    sub = _sub(status="active")
    await sj.sync_pasarguard_usage_and_status(mock_session, NS(base_url="http://h"), [sub])

    assert sub.status == "expired"
    assert sub.xui_client.is_active is False


@pytest.mark.asyncio
async def test_usage_sync_gone_strikes_to_expired(monkeypatch, mock_session):
    import apps.worker.jobs.subscriptions as sj

    # get_user returns None (404 on panel) → strike; threshold is 5.
    monkeypatch.setattr(sj, "create_pasarguard_client_for_server", _fake_cm(FakePGClient(None)))

    sub = _sub(status="active", usage_sync_failures=4)
    await sj.sync_pasarguard_usage_and_status(mock_session, NS(base_url="http://h"), [sub])

    assert sub.usage_sync_failures == 5
    assert sub.status == "expired"
    assert sub.xui_client.is_active is False


@pytest.mark.asyncio
async def test_usage_sync_isolates_one_bad_row(monkeypatch, mock_session):
    """A single malformed panel response (raises mid-loop, e.g. ValidationError)
    must NOT abort the batch — every other row still syncs."""
    import apps.worker.jobs.subscriptions as sj

    class FlakyPG:
        async def get_user(self, username):
            if username == "bad":
                raise ValueError("malformed panel response")  # stand-in for ValidationError
            return PGUserResponse(id=1, username=username, status="active", used_traffic=777, subscription_url="/sub/x")

    monkeypatch.setattr(sj, "create_pasarguard_client_for_server", _fake_cm(FlakyPG()))

    bad = _sub(status="active", xui_client=NS(panel_username="bad", username="bad", usage_bytes=0, is_active=True))
    good = _sub(status="active", xui_client=NS(panel_username="good", username="good", usage_bytes=0, is_active=True))

    # Must not raise, and the good row must still be updated.
    await sj.sync_pasarguard_usage_and_status(mock_session, NS(base_url="http://h"), [bad, good])
    assert good.used_bytes == 777
    assert good.xui_client.usage_bytes == 777


@pytest.mark.asyncio
async def test_usage_sync_updates_usage_for_active(monkeypatch, mock_session):
    import apps.worker.jobs.subscriptions as sj

    pg_user = PGUserResponse(id=1, username="u1", status="active", used_traffic=500, subscription_url="/sub/x")
    monkeypatch.setattr(sj, "create_pasarguard_client_for_server", _fake_cm(FakePGClient(pg_user)))

    sub = _sub(status="active", used_bytes=100)
    await sj.sync_pasarguard_usage_and_status(mock_session, NS(base_url="http://h"), [sub])

    assert sub.used_bytes == 500
    assert sub.usage_sync_failures == 0


# ─── username generator ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_unique_pg_username_format(mock_session):
    from services.provisioning.manager import ProvisioningManager

    mgr = ProvisioningManager(mock_session)  # scalar() returns None → first candidate is unique
    name = await mgr._generate_unique_pg_username("My VPN ✨")

    assert re.fullmatch(r"[a-z0-9_]{3,32}", name)
    assert name.startswith("myvpn_")


@pytest.mark.asyncio
async def test_generate_unique_pg_username_prefixes_when_not_alpha(mock_session):
    from services.provisioning.manager import ProvisioningManager

    mgr = ProvisioningManager(mock_session)
    name = await mgr._generate_unique_pg_username("123")

    assert re.fullmatch(r"[a-z0-9_]{3,32}", name)
    assert name.startswith("u123_")
