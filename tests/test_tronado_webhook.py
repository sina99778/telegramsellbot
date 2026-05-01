from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from apps.api.routes.webhooks.tronado import _ensure_tronado_status_matches_payment
from schemas.internal.tronado import TronadoCallbackPayload, TronadoStatusResponse


class TestTronadoSchema:
    def test_callback_accepts_documented_payload(self):
        payload = TronadoCallbackPayload(
            PaymentID="order-123",
            UserTelegramId=123456,
            Wallet="TXYZ123",
            TronAmount="12.123456",
            ActualTronAmount="12.000000",
            CallbackUrl="https://example.com/callback",
        )

        assert payload.payment_id == "order-123"
        assert payload.user_telegram_id == 123456
        assert payload.tron_amount == Decimal("12.123456")


class TestTronadoBindingValidation:
    def test_accepts_matching_payment_id_and_wallet(self):
        payment = MagicMock()
        payment.id = "payment-id"
        payment.order_id = "order-123"
        payment.pay_address = "TXYZ123"
        payment.pay_amount = Decimal("12.000000")
        status_response = TronadoStatusResponse(
            PaymentID="order-123",
            Wallet="TXYZ123",
            ActualTronAmount=Decimal("12.000000"),
            IsPaid=True,
        )

        _ensure_tronado_status_matches_payment(
            payment=payment,
            status_response=status_response,
        )

    def test_rejects_payment_id_mismatch(self):
        payment = MagicMock()
        payment.id = "payment-id"
        payment.order_id = "order-123"
        payment.pay_address = "TXYZ123"
        payment.pay_amount = Decimal("12.000000")
        status_response = TronadoStatusResponse(
            PaymentID="other-order",
            Wallet="TXYZ123",
            IsPaid=True,
        )

        with pytest.raises(HTTPException) as exc:
            _ensure_tronado_status_matches_payment(
                payment=payment,
                status_response=status_response,
            )

        assert exc.value.status_code == 403

    def test_rejects_wallet_mismatch(self):
        payment = MagicMock()
        payment.id = "payment-id"
        payment.order_id = "order-123"
        payment.pay_address = "TXYZ123"
        payment.pay_amount = Decimal("12.000000")
        status_response = TronadoStatusResponse(
            PaymentID="order-123",
            Wallet="TDifferent",
            IsPaid=True,
        )

        with pytest.raises(HTTPException) as exc:
            _ensure_tronado_status_matches_payment(
                payment=payment,
                status_response=status_response,
            )

        assert exc.value.status_code == 403

    def test_rejects_underpaid_amount(self):
        payment = MagicMock()
        payment.id = "payment-id"
        payment.order_id = "order-123"
        payment.pay_address = "TXYZ123"
        payment.pay_amount = Decimal("12.000000")
        status_response = TronadoStatusResponse(
            PaymentID="order-123",
            Wallet="TXYZ123",
            ActualTronAmount=Decimal("11.999998"),
            IsPaid=True,
        )

        with pytest.raises(HTTPException) as exc:
            _ensure_tronado_status_matches_payment(
                payment=payment,
                status_response=status_response,
            )

        assert exc.value.status_code == 403

    def test_accepts_amount_within_tolerance(self):
        payment = MagicMock()
        payment.id = "payment-id"
        payment.order_id = "order-123"
        payment.pay_address = "TXYZ123"
        payment.pay_amount = Decimal("12.000000")
        status_response = TronadoStatusResponse(
            PaymentID="order-123",
            Wallet="TXYZ123",
            ActualTronAmount=Decimal("11.999999"),
            IsPaid=True,
        )

        _ensure_tronado_status_matches_payment(
            payment=payment,
            status_response=status_response,
        )
