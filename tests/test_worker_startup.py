"""Validate real APScheduler triggers and worker startup without external I/O."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from apps.worker import main as worker


async def test_all_worker_jobs_construct_real_triggers_and_start():
    scheduler = worker.build_worker_scheduler(MagicMock())
    jobs = {job.func.__name__: job for job in scheduler.get_jobs()}
    assert len(jobs) == 15
    reconciliation = jobs["run_reconciliation_job"]
    outbox = jobs["sync_pending_panel_states"]
    assert isinstance(reconciliation.trigger, IntervalTrigger)
    assert reconciliation.trigger.interval.total_seconds() == 120
    assert outbox.trigger.interval.total_seconds() == 15
    assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
    # Starting paused validates the actual scheduler and assigns run times,
    # without delivering jobs against real infrastructure.
    scheduler.start(paused=True)
    try:
        now = datetime.now(UTC)
        assert 0 <= (outbox.next_run_time - now).total_seconds() <= 15
        assert scheduler.running
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)


async def test_worker_entrypoint_reaches_scheduler_start_and_closes_cleanly(
    monkeypatch, tmp_path, caplog,
):
    started = asyncio.Event()
    schedulers = []

    class PausedScheduler(AsyncIOScheduler):
        def start(self, paused=False):
            super().start(paused=True)
            schedulers.append(self)
            started.set()

    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker, "AsyncIOScheduler", PausedScheduler)
    monkeypatch.setattr(worker, "PremiumEmojiBot", MagicMock(return_value=bot))
    monkeypatch.setattr(worker, "IPv4AiohttpSession", MagicMock())
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", tmp_path / "worker_heartbeat")
    monkeypatch.setattr(worker, "_heartbeat_loop", AsyncMock())
    monkeypatch.setattr("core.observability.init_sentry", lambda _name: None)
    monkeypatch.setattr("core.cache_sync.register_default_caches", lambda: None)
    monkeypatch.setattr("core.cache_sync.run_cache_invalidation_listener", AsyncMock())
    caplog.set_level("INFO", logger=worker.__name__)
    task = asyncio.create_task(worker.main())
    readiness = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait(
            {task, readiness}, timeout=3, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task  # Propagate a startup error, such as an invalid trigger argument.
        assert readiness in done and not task.done()
        assert "Worker scheduler started with 15 jobs" in caplog.text
    finally:
        readiness.cancel()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await readiness
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    bot.session.close.assert_awaited_once()
    assert schedulers and not schedulers[0].running
