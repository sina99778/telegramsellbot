"""
Tests for discount code validation and consumption.
Covers: validate_code rules, use_code atomicity, expiration, plan restriction.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from repositories.discount import DiscountRepository


def _make_discount(
    *,
    code="TEST20",
    discount_percent=20,
    max_uses=3,
    used_count=0,
    is_active=True,
    expires_at=None,
    plan_id=None,
):
    dc = MagicMock()
    dc.id = uuid4()
    dc.code = code
    dc.discount_percent = discount_percent
    dc.max_uses = max_uses
    dc.used_count = used_count
    dc.is_active = is_active
    dc.expires_at = expires_at
    dc.plan_id = plan_id
    return dc


class TestValidateCode:
    """Tests for DiscountRepository.validate_code."""

    @pytest.mark.asyncio
    async def test_valid_code_returns_discount(self, mock_session):
        dc = _make_discount()
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20")
        assert result is dc

    @pytest.mark.asyncio
    async def test_nonexistent_code_returns_none(self, mock_session):
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        result = await repo.validate_code("NOTEXIST")
        assert result is None

    @pytest.mark.asyncio
    async def test_inactive_code_returns_none(self, mock_session):
        dc = _make_discount(is_active=False)
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20")
        assert result is None

    @pytest.mark.asyncio
    async def test_exhausted_code_returns_none(self, mock_session):
        dc = _make_discount(max_uses=3, used_count=3)
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_code_returns_none(self, mock_session):
        dc = _make_discount(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20")
        assert result is None

    @pytest.mark.asyncio
    async def test_future_expiry_is_valid(self, mock_session):
        dc = _make_discount(expires_at=datetime.now(timezone.utc) + timedelta(days=7))
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20")
        assert result is dc

    @pytest.mark.asyncio
    async def test_wrong_plan_returns_none(self, mock_session):
        plan_a = uuid4()
        plan_b = uuid4()
        dc = _make_discount(plan_id=plan_a)
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20", plan_id=plan_b)
        assert result is None

    @pytest.mark.asyncio
    async def test_matching_plan_is_valid(self, mock_session):
        plan_a = uuid4()
        dc = _make_discount(plan_id=plan_a)
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        result = await repo.validate_code("TEST20", plan_id=plan_a)
        assert result is dc

    @pytest.mark.asyncio
    async def test_code_is_case_insensitive(self, mock_session):
        dc = _make_discount(code="SALE50")
        repo = DiscountRepository(mock_session)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=dc))
        )
        # The repo does .strip().upper() internally
        result = await repo.validate_code("  sale50 ")
        assert result is dc


class TestUseCode:
    """Tests for DiscountRepository.use_code with FOR UPDATE locking."""

    @pytest.mark.asyncio
    async def test_use_code_increments_used_count(self, mock_session):
        dc = _make_discount(used_count=0, max_uses=3)
        locked_dc = _make_discount(used_count=0, max_uses=3)
        locked_dc.id = dc.id

        mock_session.scalar = AsyncMock(return_value=locked_dc)

        repo = DiscountRepository(mock_session)
        await repo.use_code(dc)

        assert locked_dc.used_count == 1

    @pytest.mark.asyncio
    async def test_use_code_deactivates_at_max(self, mock_session):
        dc = _make_discount(used_count=2, max_uses=3)
        locked_dc = _make_discount(used_count=2, max_uses=3)
        locked_dc.id = dc.id

        mock_session.scalar = AsyncMock(return_value=locked_dc)

        repo = DiscountRepository(mock_session)
        await repo.use_code(dc)

        assert locked_dc.used_count == 3
        assert locked_dc.is_active is False

    @pytest.mark.asyncio
    async def test_use_code_skips_when_exhausted(self, mock_session):
        """If code is already at max_uses, use_code should be a no-op."""
        dc = _make_discount(used_count=3, max_uses=3)
        locked_dc = _make_discount(used_count=3, max_uses=3)
        locked_dc.id = dc.id

        mock_session.scalar = AsyncMock(return_value=locked_dc)

        repo = DiscountRepository(mock_session)
        await repo.use_code(dc)

        # should NOT have incremented
        assert locked_dc.used_count == 3


class TestDiscountModelFieldName:
    """Regression test: model uses 'used_count', NOT 'current_uses'."""

    def test_model_has_used_count_not_current_uses(self):
        from models.discount import DiscountCode
        mapper = DiscountCode.__table__.columns
        assert "used_count" in mapper
        assert "current_uses" not in [c.name for c in mapper]
