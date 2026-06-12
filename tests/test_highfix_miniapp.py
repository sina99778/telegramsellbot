"""
Regression tests for high-severity mini-app fixes (apps/api/routes/miniapp/users.py):

7. /payments/{id}/refresh must lock the Payment row (SELECT ... FOR UPDATE)
   before calling review_gateway_payment, honoring the process_successful_payment
   lock contract in services/payment.py (mirrors the webhook handlers).
8. Banned users must be rejected by the mini-app auth dependency
   (_get_current_user) with HTTP 403, mirroring the bot's UserAccessMiddleware.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from apps.api.routes.miniapp.users import _get_current_user, refresh_payment


# ─── Section 7: refresh_payment row lock ─────────────────────────────────────


class TestRefreshPaymentRowLock:
    @pytest.fixture
    def miniapp_user(self, user_id):
        user = MagicMock()
        user.id = user_id
        user.status = "active"
        return user

    async def test_refresh_locks_payment_row_for_update(
        self, mock_session, miniapp_user, make_payment, payment_id
    ):
        """The Payment select must carry FOR UPDATE so a double-tap on refresh
        (or a race with the IPN webhook / worker) serializes on the row."""
        payment = make_payment(provider="nowpayments")
        mock_session.scalar = AsyncMock(return_value=payment)

        with (
            patch(
                "apps.api.routes.miniapp.users.review_gateway_payment",
                new=AsyncMock(return_value="finished"),
            ),
            patch("apps.api.routes.miniapp.users.PaymentView") as mock_view,
        ):
            mock_view.model_validate.return_value = {"id": str(payment_id)}
            result = await refresh_payment(payment_id, (miniapp_user, mock_session))

        assert result["ok"] is True
        stmt = mock_session.scalar.call_args.args[0]
        assert stmt._for_update_arg is not None
        assert "FOR UPDATE" in str(stmt)

    async def test_refresh_unknown_payment_404(self, mock_session, miniapp_user):
        mock_session.scalar = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await refresh_payment(uuid4(), (miniapp_user, mock_session))
        assert exc_info.value.status_code == 404


# ─── Section 8: banned users blocked at the auth dependency ──────────────────


class TestBannedUserMiniAppAuth:
    def _execute_returning(self, user):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=user)
        return AsyncMock(return_value=result)

    async def test_banned_user_rejected_403(self, mock_session):
        banned = MagicMock()
        banned.status = "banned"
        mock_session.execute = self._execute_returning(banned)

        with patch(
            "apps.api.routes.miniapp.users.validate_telegram_init_data",
            return_value=12345,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _get_current_user(
                    init_data="stub", _auth=None, _session=None, session=mock_session
                )

        assert exc_info.value.status_code == 403
        assert "محدود" in exc_info.value.detail

    async def test_active_user_allowed(self, mock_session):
        active = MagicMock()
        active.status = "active"
        mock_session.execute = self._execute_returning(active)

        with patch(
            "apps.api.routes.miniapp.users.validate_telegram_init_data",
            return_value=12345,
        ):
            user, session = await _get_current_user(
                init_data="stub", _auth=None, _session=None, session=mock_session
            )

        assert user is active
        assert session is mock_session
