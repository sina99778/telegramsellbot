"""Regression tests for the 4 critical deep-debug fixes:

1. Dashboard SPA path traversal — resolve_dashboard_file must refuse anything
   outside the dist dir (/dashboard/..%2f..%2f.env served the app's secrets).
2. Zero-usage refund — a second invocation (double-tap / stale scrollback
   message) must NOT credit the wallet again.
3. Direct-purchase delivery failure — a Telegram send error AFTER successful
   provisioning must NOT trigger a refund (user would keep money + config).
4. Volume renewal — must NOT roll cumulative panel usage into
   lifetime_used_bytes (the panel counter is never reset, so that double-counts).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ─── 1. SPA path traversal ────────────────────────────────────────────────────


def test_spa_serves_real_file_inside_dist(tmp_path):
    from apps.api.spa import resolve_dashboard_file

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "favicon.ico").write_bytes(b"icon")

    assert resolve_dashboard_file(dist, "favicon.ico") == (dist / "favicon.ico").resolve()


def test_spa_refuses_dotdot_traversal(tmp_path):
    from apps.api.spa import resolve_dashboard_file

    dist = tmp_path / "dist"
    dist.mkdir()
    secret = tmp_path / ".env"
    secret.write_text("APP_SECRET_KEY=oops")

    # the exact attack shape: /dashboard/..%2f..%2f.env decodes to "../../.env"
    assert resolve_dashboard_file(dist, "../.env") is None
    assert resolve_dashboard_file(dist, "../../.env") is None
    assert resolve_dashboard_file(dist, "a/../../.env") is None


def test_spa_refuses_absolute_path_injection(tmp_path):
    from apps.api.spa import resolve_dashboard_file

    dist = tmp_path / "dist"
    dist.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    assert resolve_dashboard_file(dist, str(outside)) is None


def test_spa_directory_is_not_served(tmp_path):
    from apps.api.spa import resolve_dashboard_file

    dist = tmp_path / "dist"
    (dist / "sub").mkdir(parents=True)
    assert resolve_dashboard_file(dist, "sub") is None
    assert resolve_dashboard_file(dist, "") is None


# ─── 2. zero-usage refund must be once-only ──────────────────────────────────


def _refund_callback_args(order_status_locked: str):
    """Build (callback, callback_data, session, admin_user, sub, locked_order)."""
    order_id = uuid4()
    sub = NS(
        id=uuid4(),
        user_id=uuid4(),
        used_bytes=0,
        status="active",
        sub_link="https://sub/x",
        order=NS(id=order_id, status="completed", amount=Decimal("5.00"), currency="USD"),
        xui_client=None,
    )
    locked_order = NS(id=order_id, status=order_status_locked, amount=Decimal("5.00"), currency="USD")
    callback = AsyncMock()
    callback_data = NS(sid=sub.id)
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[sub, locked_order])
    admin_user = NS(id=uuid4())
    return callback, callback_data, session, admin_user, sub, locked_order


@pytest.mark.asyncio
async def test_second_refund_invocation_is_rejected():
    import apps.bot.handlers.admin.subs as subs_mod

    callback, cb_data, session, admin, sub, _ = _refund_callback_args("refunded")

    with patch.object(subs_mod, "safe_edit_or_send", AsyncMock()) as reply, \
         patch("services.wallet.manager.WalletManager") as wm:
        await subs_mod.zero_usage_refund(callback, cb_data, session, admin)

    wm.assert_not_called()                      # no second credit, ever
    assert sub.status == "active"               # nothing mutated
    assert "قبلاً بازپرداخت" in reply.call_args[0][1]


@pytest.mark.asyncio
async def test_first_refund_credits_once_and_marks_refunded():
    import apps.bot.handlers.admin.subs as subs_mod

    callback, cb_data, session, admin, sub, locked = _refund_callback_args("completed")

    wm_instance = MagicMock()
    wm_instance.process_transaction = AsyncMock()
    audit = MagicMock()
    audit.log_action = AsyncMock()

    with patch.object(subs_mod, "safe_edit_or_send", AsyncMock()), \
         patch("services.wallet.manager.WalletManager", return_value=wm_instance), \
         patch.object(subs_mod, "AuditLogRepository", return_value=audit):
        await subs_mod.zero_usage_refund(callback, cb_data, session, admin)

    wm_instance.process_transaction.assert_awaited_once()
    assert locked.status == "refunded"
    assert sub.status == "cancelled"
    # and the order row was re-read under a row lock (the second scalar call)
    assert session.scalar.await_count == 2


# ─── 3. delivery failure after provisioning must NOT refund ──────────────────


@pytest.mark.asyncio
async def test_telegram_failure_after_provisioning_does_not_refund(make_payment):
    from services.payment import _handle_direct_purchase

    plan_id = uuid4()
    payment = make_payment(
        kind="direct_purchase",
        callback_payload={"plan_id": str(plan_id), "config_name": "cfg1"},
    )
    user = NS(id=payment.user_id, telegram_id=12345, wallet=NS(id=uuid4()))
    plan = NS(
        id=plan_id, price=Decimal("5.00"), currency="USD",
        volume_bytes=10 * 1024**3, duration_days=30, name="Test30", code="t30",
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    session.get = AsyncMock(return_value=plan)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("user blocked the bot"))
    bot.send_photo = AsyncMock()
    bot.session.close = AsyncMock()

    wm_instance = MagicMock()
    wm_instance.process_transaction = AsyncMock()

    provisioned = NS(subscription=NS(id=uuid4()), sub_link="https://sub/y", vless_uri="vless://abc")
    mgr_instance = MagicMock()
    mgr_instance.provision_subscription = AsyncMock(return_value=provisioned)

    with patch("services.payment.WalletManager", return_value=wm_instance), \
         patch("services.payment._get_shared_bot", return_value=bot), \
         patch("services.provisioning.manager.ProvisioningManager", return_value=mgr_instance), \
         patch("services.sales_notifications.notify_purchase", AsyncMock()), \
         patch("services.payment._process_gateway_referral_bonus", AsyncMock()):
        result = await _handle_direct_purchase(session, payment)

    assert result is True                                  # the sale stands
    # exactly ONE wallet movement: the purchase debit — never a refund credit
    assert wm_instance.process_transaction.await_count == 1
    assert wm_instance.process_transaction.call_args.kwargs["transaction_type"] == "purchase"
    # order marked provisioned, payment payload carries the marker
    order = session.add.call_args[0][0]
    assert order.status == "provisioned"
    assert payment.callback_payload.get("provisioned") is True


@pytest.mark.asyncio
async def test_provisioning_failure_still_refunds(make_payment):
    from services.payment import _handle_direct_purchase

    plan_id = uuid4()
    payment = make_payment(
        kind="direct_purchase",
        callback_payload={"plan_id": str(plan_id), "config_name": "cfg1"},
    )
    user = NS(id=payment.user_id, telegram_id=12345, wallet=NS(id=uuid4()))
    plan = NS(
        id=plan_id, price=Decimal("5.00"), currency="USD",
        volume_bytes=10 * 1024**3, duration_days=30, name="Test30", code="t30",
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    session.get = AsyncMock(return_value=plan)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()

    wm_instance = MagicMock()
    wm_instance.process_transaction = AsyncMock()

    mgr_instance = MagicMock()
    mgr_instance.provision_subscription = AsyncMock(side_effect=RuntimeError("panel down"))

    with patch("services.payment.WalletManager", return_value=wm_instance), \
         patch("services.payment._get_shared_bot", return_value=bot), \
         patch("services.provisioning.manager.ProvisioningManager", return_value=mgr_instance), \
         patch("services.payment._process_gateway_referral_bonus", AsyncMock()):
        result = await _handle_direct_purchase(session, payment)

    assert result is False
    # two wallet movements: debit then automatic refund
    types = [c.kwargs["transaction_type"] for c in wm_instance.process_transaction.call_args_list]
    assert types == ["purchase", "refund"]
    order = session.add.call_args[0][0]
    assert order.status == "refunded"


# ─── 4. volume renewal must not double-count usage ───────────────────────────


@pytest.mark.asyncio
async def test_volume_renewal_leaves_used_and_lifetime_untouched(mock_session):
    import services.renewal as rmod

    sub = NS(
        id=uuid4(), plan_id=None, status="active",
        volume_bytes=50 * 1024**3, used_bytes=30 * 1024**3, lifetime_used_bytes=7,
        ends_at=None, activated_at=None,
    )
    # plan_id None → first scalar is the XUIClientRecord lookup → no panel sync
    mock_session.scalar = AsyncMock(return_value=None)

    settings_repo = MagicMock()
    settings_repo.get_service_security_settings = AsyncMock(return_value=NS(xui_limit_ip=1))
    with patch.object(rmod, "AppSettingsRepository", return_value=settings_repo):
        await rmod.apply_renewal(
            session=mock_session, subscription=sub, renew_type="volume", amount=20,
        )

    assert sub.volume_bytes == 70 * 1024**3       # quota grew by the purchase
    assert sub.used_bytes == 30 * 1024**3          # cumulative usage untouched
    assert sub.lifetime_used_bytes == 7            # NO double-count accumulation
