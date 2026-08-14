"""
Tests for the payment processing service.
Covers: wallet credit idempotency, provisioning retry, discount consumption,
        direct purchase flow, and the provisioned flag.
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


class TestProcessSuccessfulPayment:
    """Tests for services.payment.process_successful_payment."""

    @pytest.fixture
    def wallet_topup_payment(self, make_payment):
        return make_payment(kind="wallet_topup", actually_paid=None)

    @pytest.fixture
    def direct_purchase_payment(self, make_payment, plan_id):
        return make_payment(
            kind="direct_purchase",
            actually_paid=None,
            callback_payload={
                "plan_id": str(plan_id),
                "config_name": "TestVPN",
                "discount_percent": 0,
            },
        )

    @pytest.mark.asyncio
    async def test_wallet_topup_returns_true(self, mock_session, wallet_topup_payment):
        with patch("services.payment.WalletManager") as MockWM:
            mock_wm = AsyncMock()
            MockWM.return_value = mock_wm

            from services.payment import process_successful_payment
            result = await process_successful_payment(
                session=mock_session,
                payment=wallet_topup_payment,
                amount_to_credit=Decimal("5.00"),
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_direct_purchase_returns_false_when_provisioning_returns_non_bool(self, mock_session, make_payment, plan_id):
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),
            callback_payload={"plan_id": str(plan_id)},
        )

        with patch("services.payment._handle_direct_purchase", new_callable=AsyncMock, return_value=None):
            from services.payment import process_successful_payment
            result = await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("5.00"),
            )

        assert result is False
        assert payment.callback_payload.get("provisioned") is not True

    @pytest.mark.asyncio
    async def test_direct_purchase_uses_frozen_payment_price(self, mock_session, make_payment, plan_id):
        from services.payment import _handle_direct_purchase

        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("12.00"),
            price_amount=Decimal("12.00"),
            callback_payload={"plan_id": str(plan_id), "config_name": "TestVPN"},
        )
        user = MagicMock(id=payment.user_id, telegram_id=12345, wallet=MagicMock())
        plan = MagicMock(
            id=plan_id,
            price=Decimal("99.00"),
            currency="USD",
            volume_bytes=10 * 1024**3,
            duration_days=30,
            name="Test30",
            code="t30",
        )
        provisioned = MagicMock(
            subscription=MagicMock(id=uuid4()),
            sub_link="https://sub/test",
            vless_uri="vless://test",
        )
        wallet_manager = MagicMock()
        wallet_manager.process_transaction = AsyncMock()
        provisioning_manager = MagicMock()
        provisioning_manager.provision_subscription = AsyncMock(return_value=provisioned)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.session.close = AsyncMock()
        mock_session.scalar.return_value = user
        mock_session.get.return_value = plan

        with patch("services.payment.WalletManager", return_value=wallet_manager), \
             patch("services.payment._get_shared_bot", return_value=bot), \
             patch("services.provisioning.manager.ProvisioningManager", return_value=provisioning_manager), \
             patch("services.payment._process_gateway_referral_bonus", AsyncMock()), \
             patch("core.qr.make_qr_bytes", return_value=None), \
             patch("services.sales_notifications.notify_purchase", AsyncMock()):
            result = await _handle_direct_purchase(mock_session, payment)

        assert result is True
        order = mock_session.add.call_args.args[0]
        assert order.amount == Decimal("12.00")
        wallet_manager.process_transaction.assert_awaited_once()
        assert wallet_manager.process_transaction.call_args.kwargs["amount"] == Decimal("12.00")
        provisioning_manager.provision_subscription.assert_awaited_once_with(
            user_id=user.id,
            plan_id=plan.id,
            order_id=order.id,
            config_name="TestVPN",
        )


    @pytest.mark.asyncio
    async def test_wallet_topup_credits_wallet(self, mock_session, wallet_topup_payment):
        """Wallet topup should credit wallet and set actually_paid."""
        with patch("services.payment.WalletManager") as MockWM:
            mock_wm = AsyncMock()
            mock_wm.process_transaction = AsyncMock(return_value=MagicMock())
            MockWM.return_value = mock_wm

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=wallet_topup_payment,
                amount_to_credit=Decimal("5.00"),
            )

            # Wallet should be credited
            mock_wm.process_transaction.assert_called_once()
            call_kwargs = mock_wm.process_transaction.call_args.kwargs
            assert call_kwargs["direction"] == "credit"
            assert call_kwargs["amount"] == Decimal("5.00")
            assert wallet_topup_payment.actually_paid == Decimal("5.00")

    @pytest.mark.asyncio
    async def test_idempotency_skips_wallet_credit(self, mock_session, make_payment):
        """If already_paid is set, wallet credit should be skipped."""
        payment = make_payment(kind="wallet_topup", actually_paid=Decimal("5.00"))

        with patch("services.payment.WalletManager") as MockWM:
            mock_wm = AsyncMock()
            MockWM.return_value = mock_wm

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("5.00"),
            )

            # Should NOT credit wallet again
            mock_wm.process_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_provisioning_retry_after_failure(self, mock_session, make_payment, plan_id):
        """If wallet was credited but provisioning failed, retry should attempt provisioning."""
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),  # already credited
            callback_payload={
                "plan_id": str(plan_id),
                "config_name": "TestVPN",
                "discount_percent": 0,
                # NO "provisioned" flag — means provisioning failed before
            },
        )

        with patch("services.payment.WalletManager") as MockWM, \
             patch("services.payment._handle_direct_purchase") as mock_provision:
            mock_wm = AsyncMock()
            MockWM.return_value = mock_wm
            mock_provision.return_value = True

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("5.00"),
            )

            # Wallet should NOT be credited again
            mock_wm.process_transaction.assert_not_called()
            # Provisioning SHOULD be attempted
            mock_provision.assert_called_once()
            # provisioned flag should be set
            assert payment.callback_payload.get("provisioned") is True

    @pytest.mark.asyncio
    async def test_provisioned_flag_prevents_duplicate(self, mock_session, make_payment, plan_id):
        """If provisioned=True, skip provisioning entirely."""
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),
            callback_payload={
                "plan_id": str(plan_id),
                "provisioned": True,
            },
        )

        with patch("services.payment.WalletManager") as MockWM, \
             patch("services.payment._handle_direct_purchase") as mock_provision:
            MockWM.return_value = AsyncMock()

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("5.00"),
            )

            # Should NOT attempt provisioning
            mock_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_provisioning_does_not_set_flag(self, mock_session, make_payment, plan_id):
        """If provisioning throws, provisioned flag should NOT be set."""
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),
            callback_payload={
                "plan_id": str(plan_id),
                "config_name": "VPN",
                "discount_percent": 0,
            },
        )

        with patch("services.payment.WalletManager") as MockWM, \
             patch("services.payment._handle_direct_purchase") as mock_provision:
            MockWM.return_value = AsyncMock()
            mock_provision.side_effect = RuntimeError("X-UI connection failed")

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("5.00"),
            )

            # provisioned should NOT be True
            assert payment.callback_payload.get("provisioned") is not True

    @pytest.mark.asyncio
    async def test_direct_renewal_debits_wallet_once_and_sets_flag(self, mock_session, make_payment):
        """Gateway renewal should not leave the user with a free wallet credit."""
        sub_id = uuid4()
        payment = make_payment(
            kind="direct_renewal",
            actually_paid=None,
            price_amount=Decimal("7.00"),
            callback_payload={
                "sub_id": str(sub_id),
                "renew_type": "time",
                "renew_amount": 30,
            },
        )
        subscription = MagicMock()
        subscription.id = sub_id
        subscription.status = "active"  # the IPN status gate refuses disabled subs
        mock_session.scalar.return_value = subscription

        # The IPN renewal path now takes the canonical renewal lock — fake it.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_lock(key, ttl_seconds=60):
            yield True

        with patch("services.payment.WalletManager") as MockWM, \
             patch("core.redis.distributed_lock", fake_lock), \
             patch("services.renewal.apply_renewal", new_callable=AsyncMock) as mock_apply:
            mock_wm = AsyncMock()
            mock_wm.process_transaction = AsyncMock(return_value=MagicMock())
            MockWM.return_value = mock_wm

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("7.00"),
            )

            calls = mock_wm.process_transaction.call_args_list
            assert [call.kwargs["direction"] for call in calls] == ["credit", "debit"]
            assert calls[1].kwargs["transaction_type"] == "renewal"
            assert payment.callback_payload["wallet_debited"] is True
            assert payment.callback_payload["renewal_applied"] is True
            mock_apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_direct_renewal_retry_skips_duplicate_debit(self, mock_session, make_payment):
        sub_id = uuid4()
        payment = make_payment(
            kind="direct_renewal",
            actually_paid=Decimal("7.00"),
            price_amount=Decimal("7.00"),
            callback_payload={
                "sub_id": str(sub_id),
                "renew_type": "volume",
                "renew_amount": 10,
                "wallet_debited": True,
            },
        )
        subscription = MagicMock()
        subscription.id = sub_id
        subscription.status = "active"  # the IPN status gate refuses disabled subs
        mock_session.scalar.return_value = subscription

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_lock(key, ttl_seconds=60):
            yield True

        with patch("services.payment.WalletManager") as MockWM, \
             patch("core.redis.distributed_lock", fake_lock), \
             patch("services.renewal.apply_renewal", new_callable=AsyncMock):
            mock_wm = AsyncMock()
            MockWM.return_value = mock_wm

            from services.payment import process_successful_payment
            await process_successful_payment(
                session=mock_session,
                payment=payment,
                amount_to_credit=Decimal("7.00"),
            )

            mock_wm.process_transaction.assert_not_called()
            assert payment.callback_payload["renewal_applied"] is True
