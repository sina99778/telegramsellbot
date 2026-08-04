"""Tests for live broadcast progress in Telegram.

Before this change the admin got a single "queued" reply and the broadcast ran
entirely in the background — the only way to watch it was the web dashboard
(which has no auto-refresh). Now the handler posts a placeholder message and the
worker edits it in place while sending.

These tests cover the two pieces that can silently break the broadcast itself:
the progress text builder, and the best-effort editor (which must swallow the
Telegram errors that editing a possibly-deleted message raises).
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from apps.worker.jobs.broadcast import _edit_progress, _progress_text


def _job(processed=0, failed=0, payload=None):
    return NS(
        processed_recipients=processed,
        failed_recipients=failed,
        total_recipients=0,
        payload=payload if payload is not None else {},
    )


# ── _progress_text ───────────────────────────────────────────────────────────


def test_progress_text_reports_counts_and_percent():
    import time

    job = _job(processed=40, failed=10)
    text = _progress_text(job, done=50, total=100, started_at=time.monotonic())

    assert "50%" in text
    assert "50/100" in text
    assert "40" in text  # sent
    assert "10" in text  # failed


def test_progress_bar_fills_proportionally():
    import time

    now = time.monotonic()
    empty = _progress_text(_job(), 0, 100, now)
    half = _progress_text(_job(processed=50), 50, 100, now)
    full = _progress_text(_job(processed=100), 100, 100, now)

    # 14-cell bar: 0 / 7 / 14 filled.
    assert empty.count("▰") == 0
    assert half.count("▰") == 7
    assert full.count("▰") == 14
    # Bar width is constant regardless of progress.
    for t in (empty, half, full):
        assert t.count("▰") + t.count("▱") == 14


def test_progress_text_zero_total_does_not_divide_by_zero():
    """An audience of 0 (everyone blocked the bot) must not crash the worker."""
    import time

    text = _progress_text(_job(), done=0, total=0, started_at=time.monotonic())
    assert "100%" in text  # nothing to do == done


def test_progress_text_has_no_eta_before_any_send():
    """ETA needs a rate to extrapolate from; with done=0 there is none."""
    import time

    text = _progress_text(_job(), done=0, total=100, started_at=time.monotonic())
    assert "باقی‌مانده" not in text


def test_progress_text_has_no_eta_when_finished():
    import time

    text = _progress_text(_job(processed=100), done=100, total=100, started_at=time.monotonic())
    assert "باقی‌مانده" not in text


def test_progress_text_shows_eta_mid_run():
    import time

    # 1 of 100 done, one second elapsed → ~99s remaining.
    text = _progress_text(_job(processed=1), done=1, total=100, started_at=time.monotonic() - 1.0)
    assert "باقی‌مانده" in text


def test_progress_text_uses_minutes_for_long_etas():
    import time

    # 1 of 10_000 done in 1s → ~9999s ≈ 166 minutes.
    text = _progress_text(
        _job(processed=1), done=1, total=10_000, started_at=time.monotonic() - 1.0
    )
    assert "دقیقه" in text
    assert "ثانیه" not in text


def test_progress_text_thousands_are_separated():
    import time

    text = _progress_text(
        _job(processed=1500), done=1500, total=12000, started_at=time.monotonic()
    )
    assert "1,500/12,000" in text


# ── _edit_progress ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_progress_edits_the_stored_message():
    bot = NS(edit_message_text=AsyncMock())
    job = _job(payload={"progress_chat_id": 42, "progress_message_id": 7})

    await _edit_progress(bot, job, "hello")

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=42, message_id=7, text="hello"
    )


@pytest.mark.asyncio
async def test_edit_progress_is_a_noop_without_ids():
    """Jobs created via the dashboard/API have no progress ids in their payload;
    the worker must not attempt (or crash on) an edit for them."""
    bot = NS(edit_message_text=AsyncMock())

    await _edit_progress(bot, _job(payload={}), "hello")
    bot.edit_message_text.assert_not_awaited()

    await _edit_progress(bot, _job(payload=None), "hello")
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_progress_coerces_string_ids():
    """JSONB round-trips can hand back strings; ints are required by aiogram."""
    bot = NS(edit_message_text=AsyncMock())
    job = _job(payload={"progress_chat_id": "42", "progress_message_id": "7"})

    await _edit_progress(bot, job, "hello")

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=42, message_id=7, text="hello"
    )


@pytest.mark.asyncio
async def test_edit_progress_swallows_bad_request():
    """"message is not modified" / admin deleted the message must NOT abort the
    broadcast — the send loop is the important part, the UI is decoration."""
    bot = NS(
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(method=None, message="message is not modified")
        )
    )
    job = _job(payload={"progress_chat_id": 1, "progress_message_id": 2})

    await _edit_progress(bot, job, "hello")  # must not raise
