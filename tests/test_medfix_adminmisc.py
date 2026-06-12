"""Regression tests for deep-debug findings #37, #39, #40 (admin misc).

#37 — the change-plan-inbound picker used a shared FSM key
      ("change_inbound_plan_id") to know WHICH plan to repoint, so a stale
      picker keyboard silently retargeted whichever plan was opened LAST.
      Fix: both UUIDs now travel packed (22-char base64) inside the callback
      data itself, which also stays under Telegram's 64-byte limit.

#39 — the "unlimited"/"skip" prompt buttons in the server edit flows had no
      FSM state guard: a stale tap crashed with KeyError("server_id") or,
      worse, wiped ANOTHER server's domains mid-flow. Fix: in-handler state
      checks that answer gracefully + missing-key guards in the savers.

#40 — bot admin settings handlers primed caches / published the Redis
      invalidation BEFORE the middleware commit, so every re-reader (new
      session, READ COMMITTED) re-cached the OLD value. Fix: explicit
      session.commit() before priming/publishing, mirroring the dashboard.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from apps.bot.handlers.admin.plans import (
    ChangeInboundCallback,
    _pack_uuid,
    _unpack_uuid,
    change_inbound_confirm,
)
from apps.bot.handlers.admin.servers import (
    _save_domains,
    _save_limit,
    limit_unlimited,
    skip_sub_domain,
)
from apps.bot.states.admin import ServerManageStates

import pytest


def _make_callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.answer = AsyncMock()
    return callback


def _make_state(current_state=None, data=None):
    state = MagicMock()
    state.get_state = AsyncMock(return_value=current_state)
    state.get_data = AsyncMock(return_value=data or {})
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    return state


# ─── #37: plan id travels inside the callback data ───────────────────────────


def test_pack_unpack_uuid_roundtrip():
    original = uuid4()
    packed = _pack_uuid(original)
    assert len(packed) == 22
    assert _unpack_uuid(packed) == original


def test_unpack_uuid_rejects_garbage():
    with pytest.raises(ValueError):
        _unpack_uuid("definitely-not-base64!!")


def test_change_inbound_callback_fits_telegram_limit():
    packed = ChangeInboundCallback(
        inbound_id=_pack_uuid(uuid4()),
        plan_id=_pack_uuid(uuid4()),
        page=999,
    ).pack()
    assert len(packed.encode()) <= 64
    # And it round-trips through aiogram's unpack.
    cb = ChangeInboundCallback.unpack(packed)
    assert cb.page == 999


async def test_change_inbound_confirm_targets_callback_plan(mock_session):
    """The handler must act on the plan baked into the BUTTON, not on any
    per-user FSM leftovers from a later-opened picker."""
    plan_a_id = uuid4()
    new_inbound_id = uuid4()
    plan_a = SimpleNamespace(id=plan_a_id, inbound_id=None, name="Plan A")
    new_inbound = SimpleNamespace(id=new_inbound_id, server=SimpleNamespace(name="srv2"))

    mock_session.get = AsyncMock(return_value=plan_a)
    mock_session.scalar = AsyncMock(return_value=new_inbound)

    callback = _make_callback()
    callback_data = ChangeInboundCallback(
        inbound_id=_pack_uuid(new_inbound_id),
        plan_id=_pack_uuid(plan_a_id),
        page=3,
    )
    admin_user = SimpleNamespace(id=uuid4())

    with patch("apps.bot.handlers.admin.plans.AuditLogRepository") as audit_cls, \
         patch("apps.bot.handlers.admin.plans.view_plan", new=AsyncMock()) as view_plan:
        audit_cls.return_value.log_action = AsyncMock()
        await change_inbound_confirm(callback, callback_data, mock_session, admin_user)

    # Plan A (from the callback data) got repointed — looked up by ITS id.
    assert mock_session.get.await_args.args[1] == plan_a_id
    assert plan_a.inbound_id == new_inbound_id
    # The re-render goes back to the page baked into the button.
    view_plan.assert_awaited_once()
    assert view_plan.await_args.args[1].page == 3


async def test_change_inbound_confirm_rejects_malformed_ids(mock_session):
    callback = _make_callback()
    callback_data = ChangeInboundCallback(inbound_id="garbage", plan_id="also-bad", page=1)

    with patch("apps.bot.handlers.admin.plans.safe_edit_or_send", new=AsyncMock()) as warn:
        await change_inbound_confirm(callback, callback_data, mock_session, SimpleNamespace(id=uuid4()))

    warn.assert_awaited_once()
    mock_session.get.assert_not_awaited()


# ─── #39: stale unlimited/skip buttons ────────────────────────────────────────


async def test_limit_unlimited_stale_press_answers_gracefully(mock_session):
    """No active flow (state cleared) -> alert instead of KeyError."""
    callback = _make_callback()
    state = _make_state(current_state=None)

    await limit_unlimited(callback, state, mock_session)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get("show_alert") is True
    mock_session.get.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_skip_sub_stale_press_does_not_wipe_other_flow(mock_session):
    """A stale 'skip' tap while a DIFFERENT step is active must not run the
    saver (which would NULL the other server's domains)."""
    callback = _make_callback()
    state = _make_state(
        current_state=ServerManageStates.waiting_for_config_domain.state,
        data={"server_id": str(uuid4())},
    )

    await skip_sub_domain(callback, state, mock_session)

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    mock_session.get.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_limit_unlimited_active_flow_still_saves(mock_session):
    server = SimpleNamespace(max_clients=100)
    mock_session.get = AsyncMock(return_value=server)
    callback = _make_callback()
    state = _make_state(
        current_state=ServerManageStates.waiting_for_max_clients.state,
        data={"server_id": str(uuid4())},
    )

    await limit_unlimited(callback, state, mock_session)

    assert server.max_clients is None
    state.clear.assert_awaited_once()
    callback.message.answer.assert_awaited_once()


async def test_save_limit_missing_server_id_is_graceful(mock_session):
    message = MagicMock()
    message.answer = AsyncMock()
    state = _make_state(data={})

    await _save_limit(message, state, mock_session, None)  # must not raise

    message.answer.assert_awaited_once()
    mock_session.get.assert_not_awaited()


async def test_save_domains_missing_server_id_is_graceful(mock_session):
    message = MagicMock()
    message.answer = AsyncMock()
    state = _make_state(data={})

    await _save_domains(message, state, mock_session, "sub.example.com")  # must not raise

    message.answer.assert_awaited_once()
    mock_session.get.assert_not_awaited()


# ─── #40: commit BEFORE cache prime/publish ──────────────────────────────────


async def test_toggle_button_styles_commits_before_prime_and_publish(mock_session):
    from apps.bot.handlers.admin import settings as settings_mod

    calls: list[str] = []
    mock_session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))

    repo = MagicMock()
    repo.get_button_style_settings = AsyncMock(
        return_value=SimpleNamespace(
            enabled=True, confirm="success", destructive="danger",
            navigation="primary", info="primary",
        )
    )
    repo.update_button_style_settings = AsyncMock(side_effect=lambda **kw: calls.append("update"))

    async def _prime():
        calls.append("prime")
        return {}

    async def _publish(name):
        calls.append("publish")

    callback = _make_callback()
    with patch.object(settings_mod, "AppSettingsRepository", return_value=repo), \
         patch.object(settings_mod, "_render_button_styles_panel", new=AsyncMock()), \
         patch("apps.bot.utils.button_style.clear_button_style_cache", new=MagicMock()), \
         patch("apps.bot.utils.button_style.prime_button_style_cache", new=_prime), \
         patch("core.cache_sync.publish", new=_publish):
        await settings_mod.toggle_button_styles(callback, mock_session)

    assert "commit" in calls, "handler must commit explicitly"
    assert calls.index("update") < calls.index("commit")
    assert calls.index("commit") < calls.index("prime")
    assert calls.index("commit") < calls.index("publish")


async def test_toggle_premium_emoji_commits_before_publish(mock_session):
    from apps.bot.handlers.admin import settings as settings_mod

    calls: list[str] = []
    mock_session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))

    repo = MagicMock()
    repo.get_premium_emoji_settings = AsyncMock(
        return_value=SimpleNamespace(enabled=False, emoji_map={})
    )
    repo.update_premium_emoji_settings = AsyncMock(side_effect=lambda **kw: calls.append("update"))

    async def _publish(name):
        calls.append("publish")

    callback = _make_callback()
    with patch.object(settings_mod, "AppSettingsRepository", return_value=repo), \
         patch.object(settings_mod, "clear_premium_emoji_cache", new=MagicMock()), \
         patch.object(settings_mod, "_sync_premium_icon_cache", new=AsyncMock(side_effect=lambda: calls.append("prime"))), \
         patch.object(settings_mod, "bot_settings_menu", new=AsyncMock()), \
         patch("core.cache_sync.publish", new=_publish):
        await settings_mod.toggle_premium_emoji(callback, mock_session)

    assert calls.index("update") < calls.index("commit")
    assert calls.index("commit") < calls.index("prime")
    assert calls.index("commit") < calls.index("publish")
