"""Display policy and explicit top-up units must not change the USD ledger."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from apps.api.dependencies.db import get_db_session
from apps.api.routes.miniapp.users import create_wallet_topup, router
from repositories.settings import AppSettingsRepository
from schemas.api.miniapp import TopUpRequest


@pytest.mark.parametrize("mode,rate", [("IRT", 95000), ("USD", 125000)])
async def test_config_exposes_stored_display_policy_over_http(mock_session, mode, rate):
    async def stored_setting(_model, key):
        values = {
            AppSettingsRepository.DISPLAY_CURRENCY_KEY: {"mode": mode},
            AppSettingsRepository.USD_TOMAN_RATE_KEY: {"rate": rate},
        }
        return SimpleNamespace(value_json=values[key])

    mock_session.get = AsyncMock(side_effect=stored_setting)
    app = FastAPI()
    app.include_router(router, prefix="/api/miniapp")
    app.dependency_overrides[get_db_session] = lambda: mock_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/miniapp/config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["display_currency"] == mode
    assert response.json()["toman_rate"] == rate
    assert "bot_token" not in response.json()


@pytest.mark.parametrize("method,helper", [
    ("nowpayments", "_create_nowpayments_topup"),
    ("tetrapay", "_create_tetrapay_topup"),
    ("tronado", "_create_tronado_topup"),
    ("card_to_card", "_create_card_to_card_topup"),
])
async def test_toman_topup_is_converted_once_before_gateway(mock_session, method, helper):
    user = SimpleNamespace(telegram_id=123)
    body = TopUpRequest(amount=Decimal("475000"), currency="IRT", payment_method=method)
    result = object()
    with (
        patch("apps.api.routes.miniapp.users.check_rate_limit", new=AsyncMock()),
        patch.object(AppSettingsRepository, "get_toman_rate", new=AsyncMock(return_value=95000)),
        patch(
            f"apps.api.routes.miniapp.users.{helper}", new=AsyncMock(return_value=result)
        ) as gateway,
    ):
        assert await create_wallet_topup(body, (user, mock_session)) is result
    gateway.assert_awaited_once_with(mock_session, user, Decimal("5.00"))
    assert body.amount == Decimal("475000")


async def test_legacy_topup_without_currency_keeps_usd_contract(mock_session):
    user = SimpleNamespace(telegram_id=123)
    body = TopUpRequest(amount=Decimal("5.00"), payment_method="card_to_card")
    with (
        patch("apps.api.routes.miniapp.users.check_rate_limit", new=AsyncMock()),
        patch.object(AppSettingsRepository, "get_toman_rate", new=AsyncMock()) as rate,
        patch(
            "apps.api.routes.miniapp.users._create_card_to_card_topup", new=AsyncMock()
        ) as gateway,
    ):
        await create_wallet_topup(body, (user, mock_session))
    gateway.assert_awaited_once_with(mock_session, user, Decimal("5.00"))
    rate.assert_not_awaited()


@pytest.mark.parametrize("value,rate", [
    ("100000.5", 100000), ("100000", 0), ("100000", -1),
    ("1", 100000), ("0", 100000), ("-100000", 100000),
])
async def test_invalid_toman_amount_or_rate_never_creates_invoice(mock_session, value, rate):
    user = SimpleNamespace(telegram_id=123)
    body = TopUpRequest(amount=Decimal(value), currency="IRT", payment_method="card_to_card")
    with (
        patch("apps.api.routes.miniapp.users.check_rate_limit", new=AsyncMock()),
        patch.object(AppSettingsRepository, "get_toman_rate", new=AsyncMock(return_value=rate)),
        patch(
            "apps.api.routes.miniapp.users._create_card_to_card_topup", new=AsyncMock()
        ) as gateway,
        pytest.raises(HTTPException) as exc,
    ):
        await create_wallet_topup(body, (user, mock_session))
    assert exc.value.status_code == 400
    gateway.assert_not_awaited()


def test_topup_rejects_unsupported_currency():
    with pytest.raises(ValidationError):
        TopUpRequest(amount=Decimal("5"), currency="BTC", payment_method="card_to_card")
