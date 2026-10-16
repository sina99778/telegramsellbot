"""Regressions for numeric purchase IDs in NOWPayments status responses."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from schemas.internal.nowpayments import NowPaymentsPaymentStatusResponse
from services.nowpayments.client import NowPaymentsClient, NowPaymentsClientConfig
from services.payment import review_gateway_payment


def _status_payload(**overrides):
    return {
        "payment_id": 1234567890,
        "purchase_id": 9876543210,
        "payment_status": "finished",
        "price_amount": "5.00",
        "price_currency": "usd",
        **overrides,
    }


@pytest.mark.parametrize("purchase_id", [9876543210, "9876543210", None])
def test_status_accepts_purchase_identifier_variants(purchase_id):
    status = NowPaymentsPaymentStatusResponse.model_validate(
        _status_payload(purchase_id=purchase_id)
    )
    assert status.purchase_id == purchase_id
    assert status.model_dump(mode="json")["purchase_id"] == purchase_id
    assert status.price_amount == Decimal("5.00")


def test_status_accepts_missing_purchase_identifier():
    payload = _status_payload()
    del payload["purchase_id"]
    assert NowPaymentsPaymentStatusResponse.model_validate(payload).purchase_id is None


@pytest.mark.parametrize("purchase_id", [{"id": 123}, [123]])
def test_status_rejects_structured_purchase_identifiers(purchase_id):
    with pytest.raises(ValidationError):
        NowPaymentsPaymentStatusResponse.model_validate(_status_payload(purchase_id=purchase_id))


def test_numeric_purchase_identifier_does_not_relax_amount_validation():
    with pytest.raises(ValidationError):
        NowPaymentsPaymentStatusResponse.model_validate(_status_payload(price_amount="invalid"))


@pytest.mark.parametrize(
    ("provider_status", "already_applied", "expected"),
    [
        ("finished", False, "processed"),
        ("confirmed", False, "processed"),
        ("waiting", False, "not_paid"),
        ("confirming", False, "not_paid"),
        ("partially_paid", False, "not_paid"),
        ("failed", False, "not_paid"),
        ("finished", True, "already_processed"),
        ("confirmed", True, "already_processed"),
    ],
)
async def test_review_with_numeric_purchase_id_preserves_settlement_guards(
    mock_session, make_payment, provider_status, already_applied, expected
):
    """Exercise HTTP decoding and the real reviewer, including renewal replay guards."""
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=_status_payload(payment_status=provider_status))

    payment = make_payment(
        kind="direct_renewal",
        provider_payment_id="1234567890",
        actually_paid=Decimal("5.00") if already_applied else None,
        callback_payload={"renewal_applied": True} if already_applied else {},
    )
    gateway_settings = SimpleNamespace(nowpayments_api_key="test-key")
    processor = AsyncMock()
    async with httpx.AsyncClient(
        base_url="https://payments.example/v1/", transport=httpx.MockTransport(respond)
    ) as http_client:
        client = NowPaymentsClient(
            NowPaymentsClientConfig(api_key=SecretStr("test-key")), http_client=http_client
        )
        with (
            patch(
                "services.payment.AppSettingsRepository.get_gateway_settings",
                AsyncMock(return_value=gateway_settings),
            ),
            patch("services.payment.NowPaymentsClient", return_value=client),
            patch("services.payment.process_successful_payment", processor),
        ):
            result = await review_gateway_payment(mock_session, payment)

    assert result == expected
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/payment/1234567890"
    assert payment.callback_payload["manual_review"]["purchase_id"] == 9876543210
    assert payment.payment_status == provider_status
    if expected == "processed":
        processor.assert_awaited_once_with(
            session=mock_session, payment=payment, amount_to_credit=Decimal("5.00")
        )
    else:
        processor.assert_not_awaited()
