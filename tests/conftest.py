"""
Shared test fixtures.

These tests use mock-based unit testing since the production DB uses
PostgreSQL-specific features (JSONB, UUID, FOR UPDATE) that don't work
with SQLite in-memory. Integration tests with a real Postgres can be
added later.
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def plan_id():
    return uuid4()


@pytest.fixture
def payment_id():
    return uuid4()


@pytest.fixture
def mock_session():
    """Create a mock AsyncSession with common methods."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.get = AsyncMock(return_value=None)
    # Support begin_nested as async context manager
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    return session


@pytest.fixture
def make_payment(user_id, payment_id):
    """Factory for Payment-like mock objects."""
    def _make(
        kind="wallet_topup",
        payment_status="waiting",
        actually_paid=None,
        price_amount=Decimal("5.00"),
        price_currency="USD",
        callback_payload=None,
        provider="nowpayments",
        provider_payment_id=None,
        order_id=None,
    ):
        payment = MagicMock()
        payment.id = payment_id
        payment.user_id = user_id
        payment.kind = kind
        payment.payment_status = payment_status
        payment.actually_paid = actually_paid
        payment.price_amount = price_amount
        payment.price_currency = price_currency
        payment.callback_payload = callback_payload or {}
        payment.provider = provider
        payment.provider_payment_id = provider_payment_id
        payment.order_id = order_id
        return payment
    return _make
