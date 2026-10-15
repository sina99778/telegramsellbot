"""Transactional desired-state outbox for renewal and remote revocation.

The subscription row is the outbox: limits, status and the pending marker are
committed with the financial transaction. Consumers hold its database row lock
and issue only absolute, idempotent updates. They never reset traffic counters.
"""

from __future__ import annotations

from models.subscription import Subscription


def request_panel_sync(subscription: Subscription) -> None:
    subscription.panel_sync_pending = True
    subscription.panel_sync_error = None
    subscription.panel_sync_attempts = 0
    subscription.panel_sync_attempted_at = None
