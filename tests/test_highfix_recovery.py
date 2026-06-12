"""
Regression tests for high-severity admin recovery fixes
(apps/bot/handlers/admin/recovery.py, findings 9 + 10):

Every recovery handler that reaches the money path
(process_successful_payment / review_gateway_payment) must load the
Payment row with SELECT ... FOR UPDATE, honoring the lock contract in
services/payment.py — otherwise an admin double-click or a race with the
reconciliation worker / a late IPN webhook double-debits the wallet and
provisions a duplicate config.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.bot.handlers.admin.recovery import (
    RecoveryPaymentCallback,
    recovery_retry_provisioning,
    recovery_review_gateway_payment,
)


@pytest.fixture
def admin_user(user_id):
    admin = MagicMock()
    admin.id = user_id
    return admin


@pytest.fixture
def mock_callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture
def mock_audit():
    """Patch the module-level AuditLogRepository used by the handlers."""
    with patch("apps.bot.handlers.admin.recovery.AuditLogRepository") as repo_cls:
        repo_cls.return_value.log_action = AsyncMock()
        yield repo_cls


@pytest.fixture
def mock_send():
    with patch(
        "apps.bot.handlers.admin.recovery.safe_edit_or_send", new=AsyncMock()
    ) as send:
        yield send


def _assert_locked(stmt) -> None:
    assert stmt._for_update_arg is not None
    assert "FOR UPDATE" in str(stmt)


# ─── Retry Provisioning (finding 9/10) ───────────────────────────────────────


class TestRetryProvisioningRowLock:
    async def test_retry_locks_payment_row_for_update(
        self, mock_session, mock_callback, admin_user, make_payment, payment_id,
        mock_audit, mock_send,
    ):
        """The Payment select must carry FOR UPDATE so a double-click (or a
        race with the reconciliation worker) serializes on the row before
        process_successful_payment debits/provisions."""
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),
            callback_payload={},
        )
        mock_session.scalar = AsyncMock(return_value=payment)
        process = AsyncMock()

        with patch("services.payment.process_successful_payment", new=process):
            await recovery_retry_provisioning(
                mock_callback,
                RecoveryPaymentCallback(action="retry", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        _assert_locked(mock_session.scalar.call_args.args[0])
        process.assert_awaited_once()

    async def test_retry_skips_when_already_provisioned_after_lock(
        self, mock_session, mock_callback, admin_user, make_payment, payment_id,
        mock_audit, mock_send,
    ):
        """The provisioned re-check runs on the lock-serialized row: a racer
        that lost the lock must see provisioned=True and never re-enter the
        money path."""
        payment = make_payment(
            kind="direct_purchase",
            actually_paid=Decimal("5.00"),
            callback_payload={"provisioned": True},
        )
        mock_session.scalar = AsyncMock(return_value=payment)
        process = AsyncMock()

        with patch("services.payment.process_successful_payment", new=process):
            await recovery_retry_provisioning(
                mock_callback,
                RecoveryPaymentCallback(action="retry", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        process.assert_not_awaited()

    async def test_retry_unknown_payment_no_money_path(
        self, mock_session, mock_callback, admin_user, payment_id, mock_audit, mock_send,
    ):
        mock_session.scalar = AsyncMock(return_value=None)
        process = AsyncMock()

        with patch("services.payment.process_successful_payment", new=process):
            await recovery_retry_provisioning(
                mock_callback,
                RecoveryPaymentCallback(action="retry", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        process.assert_not_awaited()


# ─── Gateway Review (finding 9/10) ───────────────────────────────────────────


class TestReviewGatewayPaymentRowLock:
    async def test_review_locks_payment_row_for_update(
        self, mock_session, mock_callback, admin_user, make_payment, payment_id,
        mock_audit, mock_send,
    ):
        """review_gateway_payment reaches process_successful_payment, so the
        handler must hold the row lock to serialize with a late IPN webhook."""
        payment = make_payment(provider="nowpayments")
        mock_session.scalar = AsyncMock(return_value=payment)
        review = AsyncMock(return_value="finished")

        with patch("services.payment.review_gateway_payment", new=review):
            await recovery_review_gateway_payment(
                mock_callback,
                RecoveryPaymentCallback(action="review", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        _assert_locked(mock_session.scalar.call_args.args[0])
        review.assert_awaited_once_with(mock_session, payment)
