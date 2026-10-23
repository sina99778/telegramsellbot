import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from apps.worker.jobs import subscriptions as jobs
from core.database import utcnow
from repositories.settings import ServiceSecuritySettings


async def test_sync_bounds_panel_requests_preserves_rows_and_isolates_errors(monkeypatch):
    active = peak = completed = 0

    async def probe(subscription, client, settings):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.001)
            if subscription.id == 3:
                raise ValueError('isolated row error')
            completed += 1
            return None
        finally:
            active -= 1

    monkeypatch.setattr(jobs, '_get_ip_abuse', probe)
    rows = [NS(id=i, plan=None, status='active', volume_bytes=0,
               ends_at=utcnow() + timedelta(days=1),
               xui_client=NS(email=f'u{i}', client_uuid=str(i),
                             xui_client_remote_id=str(i),
                             inbound=NS(xui_inbound_remote_id=1))) for i in range(80)]
    snapshot = [NS(id=1, settings={'clients': [dict(email=f'u{i}', id=str(i))
                                            for i in range(80)]},
                   client_stats=[dict(email=f'u{i}', up=i, down=10)
                                 for i in range(80)])]
    client = NS(get_inbounds=AsyncMock(return_value=snapshot))
    session = NS(flush=AsyncMock())
    expired = await jobs.sync_xui_usage_and_status(
        session, client, rows,
        ServiceSecuritySettings(xui_limit_ip=1, max_distinct_ips=3,
                                auto_disable_ip_abuse=True),
    )
    assert 1 < peak <= 8
    assert completed == 79
    assert expired is False
    assert [row.used_bytes for row in rows] == [i + 10 for i in range(80)]
    session.flush.assert_awaited_once()
    client.get_inbounds.assert_awaited_once()
