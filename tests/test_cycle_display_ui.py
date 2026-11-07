from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.api.routes.miniapp.users import (
    _admin_subscriptions,
    _serialize_admin_user_detail,
    _subscription_to_view,
)
from apps.bot.handlers.admin.config_search import _sub_detail_view
from apps.bot.handlers.admin.customers import CustSubCallback, customer_sub_detail
from apps.bot.handlers.admin.subs import _render_sub_detail, _render_user_configs, add_volume_submit
from apps.bot.handlers.user.my_configs import (
    MyConfigCallback,
    my_config_detail_handler,
    refresh_usage_handler,
)
from core.formatting import format_plan_volume, format_volume_bytes

GIB = 1024**3


def test_admin_search_detail_shows_cycle_usage():
    sub = make_sub()
    text, _ = _sub_detail_view(sub)
    assert "مصرف: 0 GB / 20 GB" in text
    assert "0%" in text


@pytest.mark.parametrize("surface", ["customer", "subscription", "list"])
async def test_admin_bot_service_displays_current_cycle(surface, mock_session):
    sub = make_sub()
    mock_session.scalar.return_value = sub
    callback = MagicMock(answer=AsyncMock())
    module = "customers" if surface == "customer" else "subs"
    with patch(
        f"apps.bot.handlers.admin.{module}.safe_edit_or_send", new_callable=AsyncMock
    ) as edit:
        if surface == "customer":
            await customer_sub_detail(callback, CustSubCallback(sub_id=sub.id), mock_session)
        elif surface == "subscription":
            await _render_sub_detail(callback, sub.id, uuid4(), 1, mock_session)
        else:
            user = SimpleNamespace(id=uuid4(), subscriptions=[sub])
            mock_session.scalar.return_value = user
            await _render_user_configs(
                callback=callback, session=mock_session, user_id=user.id, page=1
            )
    text = edit.await_args.args[1]
    assert "0 GB" in text
    assert "20 GB" in text
    assert "40 GB" not in text
    assert sub.volume_bytes == 40 * GIB
    assert sub.used_bytes == 20 * GIB
    if surface == "subscription":
        markup = edit.await_args.kwargs["reply_markup"]
        assert all(
            "بازپرداخت" not in button.text for row in markup.inline_keyboard for button in row
        )


def make_sub(*, baseline=20 * GIB, used=20 * GIB):
    return SimpleNamespace(
        id=uuid4(),
        status="active",
        source=None,
        used_bytes=used,
        volume_bytes=40 * GIB,
        lifetime_used_bytes=0,
        cycle_start_used_bytes=baseline,
        plan=None,
        plan_id=None,
        xui_client=None,
        sub_link=None,
        panel_sync_pending=False,
        panel_sync_error=None,
        starts_at=None,
        ends_at=None,
        activated_at=None,
        last_usage_sync_at=None,
        created_at=datetime.now(UTC),
        user=None,
    )


async def test_admin_volume_confirmation_shows_cycle_but_keeps_raw_accounting(mock_session):
    sub = make_sub()
    mock_session.scalar.return_value = sub
    state = AsyncMock()
    state.get_data.return_value = {"sub_id": str(sub.id)}
    message = MagicMock(text="5", answer=AsyncMock())

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield True

    async def renew(**kwargs):
        assert kwargs["subscription"].volume_bytes == 40 * GIB
        assert kwargs["renew_type"] == "volume"
        assert kwargs["amount"] == 5
        sub.volume_bytes += 5 * GIB

    with (
        patch("core.redis.distributed_lock", new=lock),
        patch("apps.bot.handlers.admin.subs.apply_renewal", new=renew),
        patch("apps.bot.handlers.admin.subs.AuditLogRepository") as audit,
    ):
        audit.return_value.log_action = AsyncMock()
        await add_volume_submit(message, state, mock_session, SimpleNamespace(id=uuid4()))
    assert audit.return_value.log_action.await_args.kwargs["payload"] == {
        "added_gb": 5.0,
        "old_bytes": 40 * GIB,
        "new_bytes": 45 * GIB,
    }
    assert "حجم جدید: 25 GB" in message.answer.await_args.args[0]
    assert sub.volume_bytes == 45 * GIB


@pytest.mark.parametrize(
    "baseline,expected_total,expected_used",
    [
        (20 * GIB, 20 * GIB, 0),
        (None, 40 * GIB, 20 * GIB),
    ],
)
def test_miniapp_service_projection_shows_current_cycle(
    baseline,
    expected_total,
    expected_used,
):
    sub = make_sub(baseline=baseline)
    view = _subscription_to_view(sub)
    assert (view.volume_bytes, view.used_bytes) == (expected_total, expected_used)
    assert sub.volume_bytes == 40 * GIB
    assert sub.used_bytes == 20 * GIB


def test_admin_user_service_detail_shows_current_cycle():
    sub = make_sub()
    user = SimpleNamespace(
        id=uuid4(),
        telegram_id=123,
        username=None,
        first_name="Test",
        last_name=None,
        role="user",
        status="active",
        is_bot_blocked=False,
        has_received_free_trial=False,
        wallet=None,
        created_at=None,
        profile=None,
        subscriptions=[sub],
        orders=[],
    )
    result = _serialize_admin_user_detail(user)["subscriptions"][0]
    assert (result["volume_bytes"], result["used_bytes"]) == (20 * GIB, 0)


async def test_admin_service_listing_shows_current_cycle(mock_session):
    sub = make_sub()
    result = MagicMock()
    result.scalars.return_value.unique.return_value.all.return_value = [sub]
    mock_session.execute.return_value = result
    rows = await _admin_subscriptions(mock_session)
    assert rows[0]["subtitle"].endswith(f"0/{20 * GIB}")


async def test_refresh_shows_fresh_cycle_usage(mock_session):
    sub = make_sub(used=19 * GIB)
    mock_session.scalar.return_value = sub
    callback = MagicMock(answer=AsyncMock())
    callback.from_user.id = 123
    raw = {"used_bytes": 21 * GIB, "total_bytes": 40 * GIB, "remaining_bytes": 19 * GIB}
    with (
        patch("apps.bot.handlers.user.my_configs.UserRepository") as repo,
        patch("apps.worker.jobs.subscriptions.get_realtime_usage", new=AsyncMock(return_value=raw)),
        patch(
            "apps.bot.handlers.user.my_configs.safe_edit_or_send", new_callable=AsyncMock
        ) as edit,
    ):
        repo.return_value.get_by_telegram_id = AsyncMock(
            return_value=SimpleNamespace(id=uuid4()),
        )
        await refresh_usage_handler(
            callback,
            MyConfigCallback(action="refresh_usage", subscription_id=sub.id),
            mock_session,
        )
    text = edit.await_args.args[1]
    assert f"حجم کل: {format_plan_volume(20 * GIB)}" in text
    assert f"مصرف شده: {format_volume_bytes(GIB)}" in text
    assert f"باقی‌مانده: {format_volume_bytes(19 * GIB)}" in text


async def test_bot_detail_and_banner_show_current_cycle(mock_session):
    sub = make_sub()
    mock_session.scalar.side_effect = [sub, SimpleNamespace(content="vless://test")]
    callback = MagicMock(answer=AsyncMock())
    callback.from_user.id = 123
    callback.message.edit_text = AsyncMock()
    with (
        patch("apps.bot.handlers.user.my_configs.UserRepository") as repo,
        patch("apps.bot.handlers.user.my_configs.AppSettingsRepository") as settings,
        patch(
            "apps.worker.jobs.subscriptions.get_realtime_usage", new=AsyncMock(return_value=None)
        ),
        patch(
            "apps.bot.handlers.user.my_configs.create_traffic_banner", return_value=None
        ) as banner,
    ):
        repo.return_value.get_by_telegram_id = AsyncMock(
            return_value=SimpleNamespace(id=uuid4()),
        )
        settings.return_value.get_user_actions_settings = AsyncMock(
            return_value=SimpleNamespace(
                transfer_enabled=False,
                refund_enabled=False,
                delete_enabled=False,
            ),
        )
        await my_config_detail_handler(
            callback,
            MyConfigCallback(action="view", subscription_id=sub.id),
            mock_session,
            MagicMock(),
        )
    text = callback.message.edit_text.await_args.args[0]
    assert f"<code>{format_plan_volume(20 * GIB)}</code>" in text
    assert f"<code>{format_volume_bytes(0)}</code>" in text
    assert banner.call_args.kwargs["total_gb"] == 20
    assert banner.call_args.kwargs["used_gb"] == 0
