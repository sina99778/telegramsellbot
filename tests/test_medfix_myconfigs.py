"""
Regression tests for medium findings #45 and #46 in
apps/bot/handlers/user/my_configs.py.

#45 — toggle_enable on a Marzban-family config: re-enabling a
pending_activation (first_use) config must restore the panel's `on_hold`
state with the plan's original on_hold_expire_duration, NOT send
status="active" (which starts the expiry timer before the user ever
connects and lets the sync job activate the DB sub).

#46 — delete_expired_config must re-validate the "expired OR
server-deleted" precondition server-side (inline keyboards stay live
forever) and lock the subscription row FOR UPDATE, instead of trusting
the callback.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.bot.handlers.user.my_configs import (
    MyConfigCallback,
    delete_expired_config,
    toggle_enable_handler,
)


@pytest.fixture
def callback():
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock()
    cb.from_user.id = 12345
    return cb


@pytest.fixture
def user():
    u = MagicMock()
    u.id = uuid4()
    return u


def _make_server(health_status="healthy", is_active=True):
    server = MagicMock()
    server.health_status = health_status
    server.is_active = is_active
    return server


def _make_sub(status="pending_activation", *, record_active=False, duration_days=30, server=None):
    """Subscription mock with an attached Marzban-family xui record."""
    sub = MagicMock()
    sub.id = uuid4()
    sub.status = status
    sub.sub_link = "https://panel.example/sub/abc"
    sub.used_bytes = 0
    sub.plan = MagicMock()
    sub.plan.duration_days = duration_days

    xui_record = MagicMock()
    xui_record.is_active = record_active
    xui_record.panel_username = "pg_user_1"
    xui_record.username = "pg_user_1"
    xui_record.inbound = MagicMock()
    xui_record.inbound.server = server if server is not None else _make_server()
    sub.xui_client = xui_record
    return sub


def _patch_user_repo(user):
    repo_cls = patch("apps.bot.handlers.user.my_configs.UserRepository")
    return repo_cls, user


async def _run_toggle(callback, sub, user, mock_session):
    """Drive toggle_enable_handler through the Marzban-family branch and
    return the PGUserModify payload passed to modify_user."""
    mock_session.scalar = AsyncMock(return_value=sub)

    client = MagicMock()
    client.modify_user = AsyncMock()
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("apps.bot.handlers.user.my_configs.UserRepository") as repo_cls, \
         patch("apps.bot.handlers.user.my_configs.record_is_marzban_family", return_value=True), \
         patch("apps.bot.handlers.user.my_configs.safe_edit_or_send", new_callable=AsyncMock) as edit, \
         patch("services.xui.runtime.ensure_inbound_server_loaded", return_value=MagicMock()), \
         patch("services.panels.marzban.marzban_client_for_server", return_value=client_cm):
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=user)
        callback_data = MyConfigCallback(action="toggle_enable", subscription_id=sub.id)
        await toggle_enable_handler(callback, callback_data, mock_session)

    client.modify_user.assert_awaited_once()
    args = client.modify_user.await_args
    return args.args[1], edit


# ─── #45: Marzban-family enable toggle vs on_hold ────────────────────────────


async def test_reenable_pending_marzban_restores_on_hold(callback, user, mock_session):
    """pending_activation + first_use: re-enable must send on_hold with the
    plan's original duration — never 'active'."""
    sub = _make_sub(status="pending_activation", record_active=False, duration_days=30)

    payload, _ = await _run_toggle(callback, sub, user, mock_session)

    assert payload.status == "on_hold"
    body = payload.to_payload()
    assert body["on_hold_expire_duration"] == 30 * 86400
    # The record flips to enabled locally.
    assert sub.xui_client.is_active is True


async def test_reenable_active_marzban_sends_active(callback, user, mock_session):
    """An already-activated sub keeps the old behavior: re-enable = active."""
    sub = _make_sub(status="active", record_active=False)

    payload, _ = await _run_toggle(callback, sub, user, mock_session)

    assert payload.status == "active"
    assert "on_hold_expire_duration" not in payload.to_payload()


async def test_disable_pending_marzban_sends_disabled(callback, user, mock_session):
    """Turning OFF is unchanged: status='disabled' even for pending subs."""
    sub = _make_sub(status="pending_activation", record_active=True)

    payload, _ = await _run_toggle(callback, sub, user, mock_session)

    assert payload.status == "disabled"
    assert sub.xui_client.is_active is False


async def test_reenable_pending_without_plan_falls_back_to_active(callback, user, mock_session):
    """No plan (no known duration): the safe fallback is the previous
    behavior — we can't reconstruct an on_hold timer without a duration."""
    sub = _make_sub(status="pending_activation", record_active=False)
    sub.plan = None

    payload, _ = await _run_toggle(callback, sub, user, mock_session)

    assert payload.status == "active"


# ─── #46: delete handler precondition re-validation ──────────────────────────


async def _run_delete(callback, sub, user, mock_session, *, strategy=None):
    mock_session.scalar = AsyncMock(return_value=sub)
    strategy = strategy or MagicMock(delete_config=AsyncMock())

    with patch("apps.bot.handlers.user.my_configs.UserRepository") as repo_cls, \
         patch("apps.bot.handlers.user.my_configs.AppSettingsRepository") as settings_cls, \
         patch("apps.bot.handlers.user.my_configs.safe_edit_or_send", new_callable=AsyncMock) as edit, \
         patch("services.xui.runtime.ensure_inbound_server_loaded", return_value=MagicMock()), \
         patch("services.panels.registry.strategy_for_record", return_value=strategy):
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=user)
        settings_cls.return_value.get_user_actions_settings = AsyncMock(
            return_value=MagicMock(delete_enabled=True)
        )
        callback_data = MyConfigCallback(action="delete", subscription_id=sub.id)
        await delete_expired_config(callback, callback_data, mock_session)

    return strategy, edit


async def test_delete_rejects_active_config_on_healthy_server(callback, user, mock_session):
    """Stale/forged callback for an ACTIVE config must be refused: no panel
    delete, status and sub_link untouched."""
    sub = _make_sub(status="active", record_active=True)

    strategy, edit = await _run_delete(callback, sub, user, mock_session)

    strategy.delete_config.assert_not_awaited()
    assert sub.status == "active"
    assert sub.sub_link == "https://panel.example/sub/abc"
    mock_session.flush.assert_not_awaited()
    # The user gets a refusal message, not the success one.
    refusal = edit.await_args.args[1]
    assert "قابل حذف نیست" in refusal


async def test_delete_allows_expired_config(callback, user, mock_session):
    """The legitimate path still works: expired config is deleted on the
    panel and cancelled locally."""
    sub = _make_sub(status="expired", record_active=True)

    strategy, _ = await _run_delete(callback, sub, user, mock_session)

    strategy.delete_config.assert_awaited_once()
    assert sub.status == "cancelled"
    assert sub.sub_link is None
    mock_session.flush.assert_awaited()


async def test_delete_allows_server_deleted_config(callback, user, mock_session):
    """A non-expired config whose server is gone is still deletable (panel
    call skipped), matching the button's rendering condition."""
    sub = _make_sub(
        status="active",
        record_active=True,
        server=_make_server(health_status="deleted", is_active=False),
    )

    strategy, _ = await _run_delete(callback, sub, user, mock_session)

    strategy.delete_config.assert_not_awaited()  # server gone: skip panel call
    assert sub.status == "cancelled"
    assert sub.sub_link is None


async def test_delete_select_locks_row_for_update(callback, user, mock_session):
    """The subscription load must take a FOR UPDATE row lock like the
    sibling money-path handlers."""
    sub = _make_sub(status="expired", record_active=True)

    await _run_delete(callback, sub, user, mock_session)

    stmt = mock_session.scalar.await_args.args[0]
    assert stmt._for_update_arg is not None
