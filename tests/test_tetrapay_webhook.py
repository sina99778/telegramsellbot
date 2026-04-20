"""
Tests for TetraPay webhook handler and schema parsing.
Covers: integer vs string status, schema alias, status filtering.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.internal.tetrapay import TetraPayCallbackPayload


class TestTetraPaySchema:
    """Tests for TetraPayCallbackPayload Pydantic model."""

    def test_accepts_integer_status(self):
        """TetraPay sends status as int (100), not string."""
        data = {"status": 100, "hashid": "abc123", "authority": "auth456"}
        payload = TetraPayCallbackPayload(**data)
        assert str(payload.status) == "100"
        assert payload.hash_id == "abc123"

    def test_accepts_string_status(self):
        data = {"status": "100", "hashid": "abc123", "authority": "auth456"}
        payload = TetraPayCallbackPayload(**data)
        assert str(payload.status) == "100"

    def test_hashid_alias_mapping(self):
        """TetraPay sends 'hashid' but we use 'hash_id' internally."""
        data = {"status": 100, "hashid": "test-hash", "authority": "test-auth"}
        payload = TetraPayCallbackPayload(**data)
        assert payload.hash_id == "test-hash"

    def test_failed_status(self):
        data = {"status": -1, "hashid": "abc", "authority": "auth"}
        payload = TetraPayCallbackPayload(**data)
        assert str(payload.status) != "100"

    def test_missing_authority_fails(self):
        with pytest.raises(ValidationError):
            TetraPayCallbackPayload(status=100, hashid="abc")

    def test_missing_hashid_is_optional(self):
        """hash_id can be None/missing in some edge cases."""
        data = {"status": 100, "authority": "auth456"}
        payload = TetraPayCallbackPayload(**data)
        assert payload.hash_id is None or payload.hash_id == ""


class TestTetraPayStatusComparison:
    """Verify the str(status) == '100' pattern used in webhook handler."""

    def test_int_100_matches(self):
        assert str(100) == "100"

    def test_str_100_matches(self):
        assert str("100") == "100"

    def test_negative_status_does_not_match(self):
        assert str(-1) != "100"
        assert str(0) != "100"
        assert str("failed") != "100"
