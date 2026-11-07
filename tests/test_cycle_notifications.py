from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from apps.worker.jobs import expiry_notifications
from models.subscription import Subscription
from models.user import User
from services import sales_notifications

GIB = 1024**3


def subscription(*, volume: int, used: int, baseline: int | None) -> Subscription:
    start = datetime(2026, 11, 7, tzinfo=UTC)
    sub = Subscription(
        id=uuid4(),
        volume_bytes=volume * GIB,
        used_bytes=used * GIB,
        lifetime_used_bytes=5 * GIB,
        starts_at=start,
        ends_at=start + timedelta(days=30),
        user=User(id=uuid4(), telegram_id=1234, is_bot_blocked=False),
    )
    sub.cycle_start_used_bytes = None if baseline is None else baseline * GIB
    return sub


@pytest.mark.parametrize(
    ("volume", "baseline", "renew_type", "amount", "expected_volume"),
    [
        (40, 25, "plan", 0, "20 گیگ"),
        (40, None, "plan", 0, "40 گیگ"),
        (50, 25, "volume", 10, "10 گیگ"),
        (0, 25, "plan", 0, None),
    ],
)
async def test_renewal_report_shows_current_period_and_actual_duration(
    monkeypatch: pytest.MonkeyPatch,
    volume: int,
    baseline: int | None,
    renew_type: str,
    amount: float,
    expected_volume: str | None,
) -> None:
    sub = subscription(volume=volume, used=20, baseline=baseline)
    for name, value in (
        ("_get_user_summary", {"id": sub.user.id, "telegram_id": 1234}),
        ("_user_wallet_balance_label", "0"),
        ("_amount_label", "5"),
        ("_config_name_for_sub_async", "test-service"),
        ("_server_label_async", "test-server"),
    ):
        monkeypatch.setattr(sales_notifications, name, AsyncMock(return_value=value))
    deliver = AsyncMock()
    monkeypatch.setattr(sales_notifications, "notify_sales_event", deliver)

    await sales_notifications.notify_renewal(
        AsyncMock(),
        AsyncMock(),
        user=sub.user,
        subscription=sub,
        renew_type=renew_type,
        amount=amount,
        price_usd=Decimal("5"),
        payment_method="wallet",
    )

    text = deliver.call_args.args[2]
    if expected_volume is None:
        assert "حجم سرویس:" not in text
    else:
        assert f"حجم سرویس: {expected_volume}" in text
    if renew_type == "plan":
        assert "مدت سرویس: 30 روز" in text
        assert "بازنشانی کامل" not in text
    else:
        assert "مقدار افزوده: <b>10</b> گیگ" in text


@pytest.mark.parametrize(
    ("volume", "used", "baseline", "expected_usage", "expected_remaining", "key"),
    [
        (40, 38, 25, "90% (18 GB از 20 GB)", "2 GB (10%)", "vol_90"),
        (50, 47, 25, "90% (27 GB از 30 GB)", "3 GB (10%)", "vol_90"),
        (40, 38, None, "95% (38 GB از 40 GB)", "2 GB (5%)", "vol_95"),
        (40, 35, 25, None, None, None),
        (0, 38, 25, None, None, None),
    ],
)
async def test_volume_warning_uses_current_period_threshold_and_text(
    monkeypatch: pytest.MonkeyPatch,
    volume: int,
    used: int,
    baseline: int | None,
    expected_usage: str | None,
    expected_remaining: str | None,
    key: str | None,
) -> None:
    sub = subscription(volume=volume, used=used, baseline=baseline)
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [sub]
    session.execute.return_value = result
    monkeypatch.setattr(
        expiry_notifications, "_load_alerted_keys_batch", AsyncMock(return_value=set())
    )
    mark = AsyncMock()
    monkeypatch.setattr(expiry_notifications, "_mark_alerted", mark)
    bot = AsyncMock()

    await expiry_notifications._send_volume_warnings(session, bot)

    if expected_usage is None:
        assert bot.send_message.call_count == 0
        assert mark.call_count == 0
        return
    text = bot.send_message.call_args.args[1]
    assert f"مصرف: {expected_usage}" in text
    assert f"باقی‌مانده: {expected_remaining}" in text
    assert mark.call_args.args[1] == f"alert.sub.{sub.id}.{key}"
