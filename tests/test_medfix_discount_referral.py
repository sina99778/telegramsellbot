"""Regression tests for the discount/referral medium fixes:

* #62 — a provisioning retry must not consume the discount code again
  (discount_consumed marker, mirroring wallet_debited).
* #69 — the referral first-purchase counter must ignore renewal orders
  (status="completed") and free trials (source="trial") in BOTH the wallet
  twin (purchase.py) and the gateway twin (payment.py), and the gateway twin
  now carries the same pay-at-most-once ledger guard.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ─── #62 discount consumed exactly once across retries ───────────────────────


def _purchase_payment(make_payment, plan_id, discount_id, *, consumed_marker=False):
    payload = {
        "plan_id": str(plan_id),
        "config_name": "cfg",
        "discount_id": str(discount_id),
        "discount_percent": 10,
    }
    if consumed_marker:
        payload["discount_consumed"] = True
    return make_payment(kind="direct_purchase", callback_payload=payload)


async def _run_direct_purchase(payment, plan, user, *, use_code_mock):
    from services.payment import _handle_direct_purchase

    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=user)
    dc = NS(id=uuid4())
    session.get = AsyncMock(side_effect=[plan, dc])

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.session.close = AsyncMock()

    wm = MagicMock()
    wm.process_transaction = AsyncMock()
    repo = MagicMock()
    repo.use_code = use_code_mock

    provisioned = NS(subscription=NS(id=uuid4()), sub_link="https://s", vless_uri="vless://x")
    mgr = MagicMock()
    mgr.provision_subscription = AsyncMock(return_value=provisioned)

    with patch("services.payment.WalletManager", return_value=wm), \
         patch("services.payment._get_shared_bot", return_value=bot), \
         patch("repositories.discount.DiscountRepository", return_value=repo), \
         patch("services.provisioning.manager.ProvisioningManager", return_value=mgr), \
         patch("services.sales_notifications.notify_purchase", AsyncMock()), \
         patch("services.payment._process_gateway_referral_bonus", AsyncMock()):
        result = await _handle_direct_purchase(session, payment)
    return result, repo


@pytest.mark.asyncio
async def test_discount_consumed_once_then_marker_set(make_payment):
    plan_id = uuid4()
    plan = NS(id=plan_id, price=Decimal("5.00"), currency="USD",
              volume_bytes=1024**3, duration_days=30, name="P", code="p")
    user = NS(id=uuid4(), telegram_id=1, wallet=NS(id=uuid4()))
    payment = _purchase_payment(make_payment, plan_id, uuid4())

    use_code = AsyncMock(return_value=NS(id=uuid4()))
    result, repo = await _run_direct_purchase(payment, plan, user, use_code_mock=use_code)

    assert result is True
    repo.use_code.assert_awaited_once()
    assert payment.callback_payload.get("discount_consumed") is True


@pytest.mark.asyncio
async def test_discount_not_consumed_again_on_retry(make_payment):
    plan_id = uuid4()
    plan = NS(id=plan_id, price=Decimal("5.00"), currency="USD",
              volume_bytes=1024**3, duration_days=30, name="P", code="p")
    user = NS(id=uuid4(), telegram_id=1, wallet=NS(id=uuid4()))
    # Retry scenario: a previous attempt already consumed the code.
    payment = _purchase_payment(make_payment, plan_id, uuid4(), consumed_marker=True)

    use_code = AsyncMock()
    result, repo = await _run_direct_purchase(payment, plan, user, use_code_mock=use_code)

    assert result is True
    repo.use_code.assert_not_awaited()


# ─── #69 referral counter ignores renewals and trials ────────────────────────


def _compiled_sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


@pytest.mark.asyncio
async def test_wallet_referral_count_excludes_renewals_and_trials():
    from apps.bot.handlers.user.purchase import _process_referral_bonus

    user = NS(id=uuid4(), telegram_id=9, referred_by_user_id=uuid4())
    captured: list = []

    session = AsyncMock()

    async def scalar(stmt):
        captured.append(stmt)
        return 2  # count != 1 → early return right after the count query

    session.scalar = AsyncMock(side_effect=scalar)

    settings_repo = MagicMock()
    settings_repo.get_referral_settings = AsyncMock(
        return_value=NS(enabled=True, referrer_bonus_usd=1.0, referee_bonus_usd=0.5)
    )
    with patch("repositories.settings.AppSettingsRepository", return_value=settings_repo):
        await _process_referral_bonus(session=session, user=user, bot=MagicMock())

    sql = _compiled_sql(captured[0])
    assert "'provisioned'" in sql and "'paid'" in sql
    assert "'completed'" not in sql          # renewals no longer counted
    assert "source" in sql and "'trial'" in sql  # trials excluded


@pytest.mark.asyncio
async def test_gateway_referral_count_excludes_renewals_and_has_ledger_guard():
    from services.payment import _process_gateway_referral_bonus

    user = NS(id=uuid4(), telegram_id=9, referred_by_user_id=uuid4())
    captured: list = []

    session = AsyncMock()

    async def scalar(stmt):
        captured.append(stmt)
        # 1st call = order count → 1 (proceed); 2nd call = ledger guard → 1 (already paid)
        return 1

    session.scalar = AsyncMock(side_effect=scalar)

    settings_repo = MagicMock()
    settings_repo.get_referral_settings = AsyncMock(
        return_value=NS(enabled=True, referrer_bonus_usd=1.0, referee_bonus_usd=0.5)
    )
    with patch("repositories.settings.AppSettingsRepository", return_value=settings_repo), \
         patch("services.payment.WalletManager") as wm, \
         patch("services.payment._get_shared_bot", return_value=MagicMock()):
        await _process_gateway_referral_bonus(session, user)

    count_sql = _compiled_sql(captured[0])
    assert "'completed'" not in count_sql and "'trial'" in count_sql
    # ledger guard fired (2 scalars) and, since already paid, NO credit happened
    assert len(captured) == 2
    guard_sql = _compiled_sql(captured[1])
    assert "referral_bonus" in guard_sql
    wm.assert_not_called()
