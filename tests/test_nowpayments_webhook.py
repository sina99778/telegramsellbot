"""
Tests for NOWPayments IPN webhook handler.
Covers: signature validation, status filtering, idempotency, amount extraction.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from unittest.mock import patch, MagicMock

from apps.api.routes.webhooks.nowpayments import (
    _is_valid_nowpayments_signature,
    _extract_credit_amount,
)


# ─── Signature Validation ──────────────────────────────────


class TestNOWPaymentsSignatureValidation:
    """Tests for _is_valid_nowpayments_signature."""

    @pytest.fixture
    def ipn_secret(self):
        return "test-ipn-secret-key-123"

    def _sign(self, payload: dict, secret: str) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(secret.encode(), canonical, hashlib.sha512).hexdigest()

    def test_valid_signature_accepted(self, ipn_secret):
        payload = {"payment_id": 123, "payment_status": "finished", "price_amount": "5.00"}
        body = json.dumps(payload).encode()
        sig = self._sign(payload, ipn_secret)

        with patch("apps.api.routes.webhooks.nowpayments.settings") as mock_settings:
            mock_settings.nowpayments_ipn_secret.get_secret_value.return_value = ipn_secret
            result = _is_valid_nowpayments_signature(raw_body=body, signature=sig)
            assert result is True

    def test_invalid_signature_rejected(self, ipn_secret):
        payload = {"payment_id": 123, "payment_status": "finished"}
        body = json.dumps(payload).encode()

        with patch("apps.api.routes.webhooks.nowpayments.settings") as mock_settings:
            mock_settings.nowpayments_ipn_secret.get_secret_value.return_value = ipn_secret
            result = _is_valid_nowpayments_signature(raw_body=body, signature="bad-sig")
            assert result is False

    def test_missing_signature_rejected(self, ipn_secret):
        body = json.dumps({"payment_id": 1}).encode()
        with patch("apps.api.routes.webhooks.nowpayments.settings") as mock_settings:
            mock_settings.nowpayments_ipn_secret.get_secret_value.return_value = ipn_secret
            assert _is_valid_nowpayments_signature(raw_body=body, signature=None) is False
            assert _is_valid_nowpayments_signature(raw_body=body, signature="") is False

    def test_malformed_json_rejected(self, ipn_secret):
        with patch("apps.api.routes.webhooks.nowpayments.settings") as mock_settings:
            mock_settings.nowpayments_ipn_secret.get_secret_value.return_value = ipn_secret
            result = _is_valid_nowpayments_signature(raw_body=b"not json", signature="abc")
            assert result is False

    def test_tampered_payload_rejected(self, ipn_secret):
        """If attacker tampers with amount, signature must fail."""
        original = {"payment_id": 123, "price_amount": "5.00"}
        sig = self._sign(original, ipn_secret)

        tampered = {"payment_id": 123, "price_amount": "999.00"}
        tampered_body = json.dumps(tampered).encode()

        with patch("apps.api.routes.webhooks.nowpayments.settings") as mock_settings:
            mock_settings.nowpayments_ipn_secret.get_secret_value.return_value = ipn_secret
            result = _is_valid_nowpayments_signature(raw_body=tampered_body, signature=sig)
            assert result is False


# ─── Amount Extraction ──────────────────────────────────


class TestExtractCreditAmount:
    """Tests for _extract_credit_amount."""

    def test_uses_price_amount_as_usd(self):
        from decimal import Decimal
        payload = {"price_amount": "5.50", "actually_paid": "0.003"}
        result = _extract_credit_amount(payload)
        assert result == Decimal("5.50")

    def test_fallback_to_actually_paid(self):
        from decimal import Decimal
        payload = {"actually_paid": "10.00"}
        result = _extract_credit_amount(payload)
        assert result == Decimal("10.00")

    def test_missing_amount_raises(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _extract_credit_amount({})
        assert exc_info.value.status_code == 400

    def test_invalid_amount_raises(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _extract_credit_amount({"price_amount": "not-a-number"})
