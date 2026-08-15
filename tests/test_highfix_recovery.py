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
from fastapi import HTTPException

from apps.api.routes.dashboard.receipts import approve_receipt
from apps.bot.handlers.admin.recovery import (
    RecoveryPaymentCallback,
    _delivery_status,
    _is_payment_fulfilled,
    _stuck_payment_condition,
    recovery_retry_provisioning,
    recovery_review_gateway_payment,
)
from apps.bot.handlers.admin.manual_payments import approve_manual_payment


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

    async def test_retry_supports_renewal_and_reports_false_result(
        self, mock_session, mock_callback, admin_user, make_payment, payment_id,
        mock_audit, mock_send,
    ):
        payment = make_payment(
            kind="direct_renewal",
            actually_paid=Decimal("5.00"),
            callback_payload={"sub_id": str(payment_id)},
        )
        mock_session.scalar = AsyncMock(return_value=payment)
        process = AsyncMock(return_value=False)

        with patch("services.payment.process_successful_payment", new=process):
            await recovery_retry_provisioning(
                mock_callback,
                RecoveryPaymentCallback(action="retry", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        process.assert_awaited_once()
        assert "هنوز تکمیل نشده" in mock_send.await_args.args[1]
        payload = mock_audit.return_value.log_action.await_args.kwargs["payload"]
        assert payload == {"result": "failed"}

    async def test_retry_reports_success_only_for_true_result(
        self, mock_session, mock_callback, admin_user, make_payment, payment_id,
        mock_audit, mock_send,
    ):
        payment = make_payment(
            kind="direct_renewal",
            actually_paid=Decimal("5.00"),
            callback_payload={"sub_id": str(payment_id)},
        )
        mock_session.scalar = AsyncMock(return_value=payment)
        process = AsyncMock(return_value=True)

        with patch("services.payment.process_successful_payment", new=process):
            await recovery_retry_provisioning(
                mock_callback,
                RecoveryPaymentCallback(action="retry", payment_id=payment_id),
                mock_session,
                admin_user,
            )

        assert "موفق بود" in mock_send.await_args.args[1]
        payload = mock_audit.return_value.log_action.await_args.kwargs["payload"]
        assert payload == {"result": "success"}

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


@pytest.mark.parametrize("kind", ["direct_purchase", "direct_renewal"])
async def test_manual_approval_commits_paid_but_unfulfilled_direct_payment(
    kind, mock_session, mock_callback, make_payment, mock_send,
):
    payment = make_payment(
        kind=kind,
        payment_status="pending_approval",
        provider="card_to_card",
        callback_payload={},
    )
    mock_session.scalar = AsyncMock(return_value=payment)
    mock_callback.data = f"mp:ok:final:{payment.id}"

    async def process_paid_but_unfulfilled(**kwargs):
        kwargs["payment"].payment_status = "finished"
        kwargs["payment"].actually_paid = kwargs["amount_to_credit"]
        return False

    process = AsyncMock(side_effect=process_paid_but_unfulfilled)

    with patch("apps.bot.handlers.admin.manual_payments.process_successful_payment", new=process), \
         patch("apps.bot.handlers.admin.manual_payments.safe_edit_caption_or_text", new=mock_send):
        await approve_manual_payment(mock_callback, mock_session)

    assert payment.payment_status == "finished"
    assert payment.actually_paid == Decimal("5.00")
    mock_session.commit.assert_awaited_once()
    mock_session.rollback.assert_not_awaited()
    message = mock_send.await_args.args[1]
    assert "پرداخت ثبت شد" in message
    assert "برای تلاش مجدد باز مانده است" in message
    assert "تأیید پرداخت ناموفق بود" not in message


async def test_dashboard_approval_commits_paid_but_unfulfilled_direct_payment(
    mock_session, make_payment, payment_id,
):
    payment = make_payment(
        kind="direct_purchase",
        payment_status="pending_approval",
        provider="card_to_card",
        callback_payload={},
    )
    admin = MagicMock(username="dashboard-admin")
    mock_session.scalar = AsyncMock(return_value=payment)

    async def process_paid_but_unfulfilled(**kwargs):
        kwargs["payment"].payment_status = "finished"
        kwargs["payment"].actually_paid = kwargs["amount_to_credit"]
        return False

    process = AsyncMock(side_effect=process_paid_but_unfulfilled)

    with patch("apps.api.routes.dashboard.receipts.process_successful_payment", new=process), \
         patch("apps.api.routes.dashboard.receipts.AuditLogRepository") as audit_repo:
        audit_repo.return_value.log_action = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await approve_receipt(payment_id, (admin, mock_session))

    assert exc_info.value.status_code == 409
    assert payment.payment_status == "finished"
    assert payment.actually_paid == Decimal("5.00")
    mock_session.rollback.assert_not_awaited()
    mock_session.commit.assert_awaited_once()
    assert payment.callback_payload["approved_by_dashboard_admin"] == "dashboard-admin"
    assert payment.callback_payload["approved_at"]
    assert "پرداخت تأیید و ثبت شد" in exc_info.value.detail


async def test_dashboard_approval_stamps_admin_and_commits_direct_payment(
    mock_session, make_payment, payment_id,
):
    payment = make_payment(
        kind="direct_purchase",
        payment_status="pending_approval",
        provider="card_to_card",
        callback_payload={},
    )
    admin = MagicMock(username="dashboard-admin")
    mock_session.scalar = AsyncMock(return_value=payment)
    process = AsyncMock(return_value=True)

    with patch("apps.api.routes.dashboard.receipts.process_successful_payment", new=process), \
         patch("apps.api.routes.dashboard.receipts.AuditLogRepository") as audit_repo:
        audit_repo.return_value.log_action = AsyncMock()
        result = await approve_receipt(payment_id, (admin, mock_session))

    assert result == {"ok": True, "fulfilled": True}
    assert payment.callback_payload["approved_by_dashboard_admin"] == "dashboard-admin"
    assert payment.callback_payload["approved_at"]
    mock_session.rollback.assert_not_awaited()
    mock_session.commit.assert_awaited_once()


def test_stuck_condition_covers_purchase_and_non_refused_renewal():
    compiled = _stuck_payment_condition().compile()
    sql = str(compiled)
    values = set(compiled.params.values())

    assert "direct_purchase" in values
    assert "direct_renewal" in values
    assert "provisioned" in values
    assert "renewal_applied" in values
    assert "renewal_refused" in values
    assert "IS false" in sql


def test_recovery_delivery_status_uses_kind_specific_marker(make_payment):
    purchase = make_payment(kind="direct_purchase", callback_payload={"provisioned": False})
    renewal = make_payment(kind="direct_renewal", callback_payload={"renewal_applied": False})

    assert _is_payment_fulfilled(purchase) is False
    assert _delivery_status(purchase) == "تحویل: انجام نشده"
    assert _is_payment_fulfilled(renewal) is False
    assert _delivery_status(renewal) == "تمدید: اعمال نشده"


def test_recovery_delivery_status_reports_completed_renewal(make_payment):
    renewal = make_payment(kind="direct_renewal", callback_payload={"renewal_applied": True})

    assert _is_payment_fulfilled(renewal) is True
    assert _delivery_status(renewal) == "تمدید: اعمال شده"
