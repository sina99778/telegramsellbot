"""
Regression tests for worker messaging jobs (deep-debug findings 15 & 16).

15: broadcast delivery progress (payload.delivered_user_ids) must be assigned
    as a NEW dict each iteration — re-assigning the same in-place-mutated dict
    is invisible to SQLAlchemy JSONB change detection after the first flush,
    so a worker restart would re-spam the whole audience.
16: retargeting must rate-gate sends, survive TelegramRetryAfter (sleep and
    retry instead of aborting the campaign), and isolate per-user send errors
    so is_bot_blocked updates are never rolled back.
"""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from apps.worker.jobs.broadcast import process_broadcast_queue
from apps.worker.jobs.retargeting import process_retargeting_campaigns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBroadcastJob:
    """BroadcastJob stand-in that records every `payload` assignment."""

    def __init__(self, payload=None):
        self.status = "queued"
        self.message_type = "text"
        self.text = "hello"
        self.media_file_id = None
        self.media_caption = None
        self.total_recipients = 0
        self.processed_recipients = 0
        self.failed_recipients = 0
        self.finished_at = None
        self._payload = payload or {}
        self.payload_assignments: list[dict] = []
        self.payload_snapshots: list[dict] = []

    @property
    def payload(self):
        return self._payload

    @payload.setter
    def payload(self, value):
        self.payload_assignments.append(value)
        self.payload_snapshots.append(copy.deepcopy(value))
        self._payload = value


def make_user(telegram_id: int):
    user = MagicMock()
    user.id = uuid4()
    user.telegram_id = telegram_id
    user.is_bot_blocked = False
    user.subscriptions = []
    return user


def scalars_all_result(items):
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def scalars_unique_all_result(items):
    result = MagicMock()
    result.scalars.return_value.unique.return_value.all.return_value = items
    return result


def make_retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=seconds)


def make_forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=MagicMock(), message="bot was blocked by the user")


# ---------------------------------------------------------------------------
# Finding 15 — broadcast payload change detection
# ---------------------------------------------------------------------------


async def test_broadcast_assigns_fresh_payload_dict_each_send(mock_session):
    """Every progress write must assign a NEW dict object so SQLAlchemy's
    JSONB compare_values sees a change (the old code reassigned the same
    mutated dict, freezing delivered_user_ids at the first recipient)."""
    job = FakeBroadcastJob()
    users = [make_user(100 + i) for i in range(3)]
    mock_session.execute = AsyncMock(
        side_effect=[scalars_all_result([job]), scalars_all_result(users)]
    )
    bot = AsyncMock()

    with patch("apps.worker.jobs.broadcast._global_rate_gate", new=AsyncMock()):
        await process_broadcast_queue(mock_session, bot)

    assert len(job.payload_assignments) == 3
    # Each assignment must be a distinct object (all are kept alive here, so
    # id() uniqueness is a valid identity check).
    ids = [id(p) for p in job.payload_assignments]
    assert len(set(ids)) == len(ids), "payload was re-assigned the same dict object"
    # No assigned dict may be mutated in place afterwards — its content must
    # still equal the snapshot taken at assignment time.
    for assigned, snapshot in zip(job.payload_assignments, job.payload_snapshots):
        assert assigned == snapshot, "a previously-assigned payload dict was mutated in place"
    # Progress must actually accumulate across iterations.
    expected_ids = sorted(str(u.id) for u in users)
    assert job.payload["delivered_user_ids"] == expected_ids
    assert job.payload_assignments[0]["delivered_user_ids"] == [str(users[0].id)]
    assert job.processed_recipients == 3
    assert job.status == "finished"


async def test_broadcast_resume_skips_already_delivered(mock_session):
    """delivered_user_ids persisted from a previous run must be honoured."""
    users = [make_user(200 + i) for i in range(3)]
    job = FakeBroadcastJob(payload={"delivered_user_ids": [str(users[0].id)]})
    mock_session.execute = AsyncMock(
        side_effect=[scalars_all_result([job]), scalars_all_result(users)]
    )
    bot = AsyncMock()

    with patch("apps.worker.jobs.broadcast._global_rate_gate", new=AsyncMock()):
        await process_broadcast_queue(mock_session, bot)

    sent_chat_ids = [call.kwargs["chat_id"] for call in bot.send_message.call_args_list]
    assert users[0].telegram_id not in sent_chat_ids
    assert sorted(sent_chat_ids) == [users[1].telegram_id, users[2].telegram_id]
    # Final payload keeps the pre-existing id plus the two new ones.
    assert job.payload["delivered_user_ids"] == sorted(str(u.id) for u in users)


async def test_broadcast_forbidden_marks_blocked_and_records_delivery(mock_session):
    job = FakeBroadcastJob()
    users = [make_user(300), make_user(301)]
    mock_session.execute = AsyncMock(
        side_effect=[scalars_all_result([job]), scalars_all_result(users)]
    )
    bot = AsyncMock()

    async def fake_send(chat_id, text):
        if chat_id == 300:
            raise make_forbidden()

    bot.send_message = AsyncMock(side_effect=fake_send)

    with patch("apps.worker.jobs.broadcast._global_rate_gate", new=AsyncMock()):
        await process_broadcast_queue(mock_session, bot)

    assert users[0].is_bot_blocked is True
    assert job.failed_recipients == 1
    assert job.processed_recipients == 1
    # Blocked user is still marked delivered so we never retry them forever.
    assert job.payload["delivered_user_ids"] == sorted(str(u.id) for u in users)


# ---------------------------------------------------------------------------
# Finding 16 — retargeting flood-wait / error isolation
# ---------------------------------------------------------------------------


def make_retargeting_settings(enabled=True, days=7, message="msg"):
    settings = MagicMock()
    settings.enabled = enabled
    settings.days = days
    settings.message = message
    return settings


def patch_retargeting_settings(settings):
    repo = MagicMock()
    repo.get_retargeting_settings = AsyncMock(return_value=settings)
    return patch(
        "apps.worker.jobs.retargeting.AppSettingsRepository",
        return_value=repo,
    )


async def test_retargeting_retry_after_does_not_abort_campaign(mock_session):
    """A flood-wait on one user must sleep retry_after, retry, and keep going."""
    users = [make_user(1), make_user(2), make_user(3)]
    mock_session.execute = AsyncMock(return_value=scalars_unique_all_result(users))
    bot = AsyncMock()
    attempts: dict[int, int] = {}

    async def fake_send(chat_id, text):
        attempts[chat_id] = attempts.get(chat_id, 0) + 1
        if chat_id == 1 and attempts[chat_id] == 1:
            raise make_retry_after(3)

    bot.send_message = AsyncMock(side_effect=fake_send)

    with patch_retargeting_settings(make_retargeting_settings()), patch(
        "apps.worker.jobs.retargeting._global_rate_gate", new=AsyncMock()
    ), patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await process_retargeting_campaigns(mock_session, bot)

    # Flooded user retried once, the rest of the audience still got the message.
    assert attempts == {1: 2, 2: 1, 3: 1}
    mock_sleep.assert_awaited_once_with(4)  # retry_after + 1
    mock_session.flush.assert_awaited()


async def test_retargeting_rate_gate_called_per_send(mock_session):
    users = [make_user(10), make_user(11)]
    mock_session.execute = AsyncMock(return_value=scalars_unique_all_result(users))
    bot = AsyncMock()

    with patch_retargeting_settings(make_retargeting_settings()), patch(
        "apps.worker.jobs.retargeting._global_rate_gate", new=AsyncMock()
    ) as gate:
        await process_retargeting_campaigns(mock_session, bot)

    assert gate.await_count == 2


async def test_retargeting_forbidden_sets_block_flag_and_continues(mock_session):
    users = [make_user(21), make_user(22)]
    mock_session.execute = AsyncMock(return_value=scalars_unique_all_result(users))
    bot = AsyncMock()

    async def fake_send(chat_id, text):
        if chat_id == 21:
            raise make_forbidden()

    bot.send_message = AsyncMock(side_effect=fake_send)

    with patch_retargeting_settings(make_retargeting_settings()), patch(
        "apps.worker.jobs.retargeting._global_rate_gate", new=AsyncMock()
    ):
        await process_retargeting_campaigns(mock_session, bot)

    assert users[0].is_bot_blocked is True
    assert users[1].is_bot_blocked is False
    assert bot.send_message.await_count == 2
    mock_session.flush.assert_awaited()


async def test_retargeting_generic_send_error_is_isolated(mock_session):
    """An arbitrary per-user failure must not abort the run or skip the flush
    (which would roll back is_bot_blocked flags set for other users)."""
    users = [make_user(31), make_user(32), make_user(33)]
    mock_session.execute = AsyncMock(return_value=scalars_unique_all_result(users))
    bot = AsyncMock()

    async def fake_send(chat_id, text):
        if chat_id == 31:
            raise RuntimeError("boom")
        if chat_id == 32:
            raise make_forbidden()

    bot.send_message = AsyncMock(side_effect=fake_send)

    with patch_retargeting_settings(make_retargeting_settings()), patch(
        "apps.worker.jobs.retargeting._global_rate_gate", new=AsyncMock()
    ):
        # Must not raise.
        await process_retargeting_campaigns(mock_session, bot)

    assert users[1].is_bot_blocked is True
    assert bot.send_message.await_count == 3
    mock_session.flush.assert_awaited()


async def test_retargeting_disabled_sends_nothing(mock_session):
    bot = AsyncMock()
    with patch_retargeting_settings(make_retargeting_settings(enabled=False)):
        await process_retargeting_campaigns(mock_session, bot)

    bot.send_message.assert_not_awaited()
    mock_session.execute.assert_not_awaited()
