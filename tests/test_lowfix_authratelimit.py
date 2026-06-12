"""
Regression tests for low-severity dashboard-login rate-limit fixes
(apps/api/routes/dashboard/auth.py):

71/72. The in-memory _LOGIN_FAILURES dict is keyed by attacker-controlled
    usernames from unauthenticated POST /login requests and previously grew
    without bound (slow memory-exhaustion DoS): _check_login_rate created a
    key per checked username via setdefault, pruning only emptied deques but
    never removed keys, and the sole deletion path was a SUCCESSFUL login.
    Now: checking never creates keys, expired entries/empty buckets are
    pruned on every check, and a hard cap evicts the oldest-inserted keys.
    Rate-limit semantics for live (in-window) entries are unchanged.
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from apps.api.routes.dashboard import auth as auth_mod
from apps.api.routes.dashboard.auth import (
    _LOGIN_FAIL_THRESHOLD,
    _LOGIN_FAIL_WINDOW,
    _LOGIN_FAILURES,
    _check_login_rate,
    _clear_login_failures,
    _record_login_failure,
)


@pytest.fixture(autouse=True)
def clean_failures():
    """Each test starts and ends with an empty rate-limit map."""
    _LOGIN_FAILURES.clear()
    yield
    _LOGIN_FAILURES.clear()


# ─── Sections 71/72: bounded memory ──────────────────────────────────────────


class TestNoUnboundedGrowth:
    def test_check_does_not_create_dict_entry(self):
        """Merely checking an unknown username must not allocate a bucket
        (the old setdefault created one permanent key per probe)."""
        assert _check_login_rate("ghost") == 0
        assert "ghost" not in _LOGIN_FAILURES
        assert len(_LOGIN_FAILURES) == 0

    def test_expired_buckets_pruned_on_any_check(self):
        """Stale entries for OTHER usernames are swept on every check."""
        stale = time.time() - _LOGIN_FAIL_WINDOW - 10
        for i in range(50):
            _LOGIN_FAILURES[f"sprayed-{i}"] = deque([stale])

        _check_login_rate("whoever")

        assert len(_LOGIN_FAILURES) == 0

    def test_partial_prune_keeps_live_timestamps(self):
        """A bucket with mixed old/new timestamps keeps only the live ones."""
        now = time.time()
        _LOGIN_FAILURES["mixed"] = deque(
            [now - _LOGIN_FAIL_WINDOW - 5, now - _LOGIN_FAIL_WINDOW - 1, now - 1]
        )

        _check_login_rate("other")

        assert list(_LOGIN_FAILURES) == ["mixed"]
        assert len(_LOGIN_FAILURES["mixed"]) == 1

    def test_hard_cap_evicts_oldest_inserted_keys(self, monkeypatch):
        """Beyond the cap, the oldest-inserted usernames are evicted so the
        dict can never exceed _LOGIN_FAILURES_MAX_KEYS entries."""
        monkeypatch.setattr(auth_mod, "_LOGIN_FAILURES_MAX_KEYS", 5)
        for i in range(8):
            _record_login_failure(f"user-{i}")

        assert len(_LOGIN_FAILURES) == 5
        # Oldest three were evicted; newest five survive.
        assert list(_LOGIN_FAILURES) == [f"user-{i}" for i in range(3, 8)]

    def test_cap_does_not_evict_on_repeat_failures_of_existing_key(
        self, monkeypatch
    ):
        """Re-failing an already-tracked username must not trigger eviction
        (dict size does not grow)."""
        monkeypatch.setattr(auth_mod, "_LOGIN_FAILURES_MAX_KEYS", 3)
        for i in range(3):
            _record_login_failure(f"user-{i}")

        _record_login_failure("user-0")

        assert len(_LOGIN_FAILURES) == 3
        assert len(_LOGIN_FAILURES["user-0"]) == 2

    def test_spray_of_distinct_usernames_stays_bounded(self, monkeypatch):
        """End-to-end attacker pattern: check + record per random username
        never grows the dict beyond the cap."""
        monkeypatch.setattr(auth_mod, "_LOGIN_FAILURES_MAX_KEYS", 10)
        for i in range(100):
            key = f"rand-{i}"
            assert _check_login_rate(key) == 0
            _record_login_failure(key)

        assert len(_LOGIN_FAILURES) <= 10


# ─── Rate-limit semantics must be unchanged for live entries ─────────────────


class TestSemanticsPreserved:
    def test_under_threshold_allowed(self):
        for _ in range(_LOGIN_FAIL_THRESHOLD - 1):
            _record_login_failure("alice")
        assert _check_login_rate("alice") == 0

    def test_at_threshold_blocked_with_retry_after(self):
        for _ in range(_LOGIN_FAIL_THRESHOLD):
            _record_login_failure("alice")
        retry_after = _check_login_rate("alice")
        assert 1 <= retry_after <= _LOGIN_FAIL_WINDOW + 1

    def test_expired_failures_no_longer_count(self):
        stale = time.time() - _LOGIN_FAIL_WINDOW - 10
        _LOGIN_FAILURES["alice"] = deque([stale] * _LOGIN_FAIL_THRESHOLD)

        assert _check_login_rate("alice") == 0
        # Bucket fully expired -> key removed too.
        assert "alice" not in _LOGIN_FAILURES

    def test_retry_after_based_on_oldest_live_failure(self):
        now = time.time()
        age = 100  # seconds ago
        _LOGIN_FAILURES["alice"] = deque(
            [now - age] * _LOGIN_FAIL_THRESHOLD
        )
        retry_after = _check_login_rate("alice")
        # ~ window - age (+1), allow slack for execution time.
        assert abs(retry_after - (_LOGIN_FAIL_WINDOW - age + 1)) <= 2

    def test_successful_login_clears_bucket(self):
        for _ in range(_LOGIN_FAIL_THRESHOLD):
            _record_login_failure("alice")
        _clear_login_failures("alice")
        assert "alice" not in _LOGIN_FAILURES
        assert _check_login_rate("alice") == 0
