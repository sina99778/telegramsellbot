"""The orphaned-custom-plan cleanup must only ever target abandoned custom plans,
and must never delete one still referenced by an order or a subscription. The DB
test suite is mock-based (no Postgres), so we assert the DELETE statement itself
carries every safety guard rather than executing it."""
from __future__ import annotations

from datetime import datetime, timezone

from apps.worker.jobs.plan_cleanup import build_orphan_custom_plan_delete


def _sql() -> str:
    stmt = build_orphan_custom_plan_delete(datetime(2020, 1, 1, tzinfo=timezone.utc))
    return str(stmt.compile()).lower()


def test_delete_targets_only_the_plans_table():
    assert "delete from plans" in _sql()


def test_delete_is_scoped_to_custom_prefix_and_age():
    sql = _sql()
    assert "like" in sql            # code LIKE 'custom\_%'
    assert "created_at" in sql      # age cutoff


def test_delete_excludes_plans_referenced_by_orders_and_subscriptions():
    """The whole point: a plan with an order (RESTRICT FK) or a subscription
    (SET NULL FK) must be protected by NOT EXISTS guards."""
    sql = _sql()
    assert sql.count("exists") >= 2
    assert "not" in sql
    assert "orders" in sql
    assert "subscriptions" in sql
