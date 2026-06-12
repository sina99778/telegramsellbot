"""
Regression tests for medium-severity plan/data fixes:

31. scripts/migrations/999_auto_sync_columns.py: _column_ddl rendered string
    server_defaults unquoted — `DEFAULT user` compiles to the USER keyword
    (current_user!) and `DEFAULT pending_activation` / `DEFAULT {}` are DDL
    syntax errors that abort the whole deploy. Plain-str defaults must be
    rendered as single-quoted SQL literals; text()/SQL-expression defaults
    must pass through untouched.
32. apps/api/routes/dashboard/plans.py: DELETE /plans/{id} only guarded on
    Subscription count, but Order.plan_id is ondelete=RESTRICT and orders
    without a subscription are normal (failed/refunded purchases) — the
    commit blew up with IntegrityError as a raw 500. Must refuse with a
    clean Persian 400 instead.
59. Same route: DiscountCode.plan_id is ondelete=SET NULL and a NULL plan_id
    means "valid for every plan" — deleting a plan silently widened its
    plan-restricted discount codes to the whole shop. The delete path must
    deactivate those codes BEFORE deleting the plan.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Boolean, Column, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB

from apps.api.routes.dashboard.plans import delete_plan


# ─── Section 31: _column_ddl default-literal quoting ─────────────────────────

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "migrations" / "999_auto_sync_columns.py"
)
_spec = importlib.util.spec_from_file_location(
    "migration_999_auto_sync_columns", _SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
_auto_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_auto_sync)
_column_ddl = _auto_sync._column_ddl


class TestColumnDDLDefaultQuoting:
    def test_keyword_colliding_string_default_is_quoted(self):
        """server_default="user" must NOT compile to the USER keyword
        (= current_user) — it must be the quoted literal 'user'."""
        col = Column("role", String(20), nullable=False, server_default="user")
        ddl = _column_ddl("users", col)
        assert ddl == "ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user'"

    def test_plain_word_string_default_is_quoted(self):
        """Bare identifiers like pending_activation are undefined-column
        errors in PG unless quoted."""
        col = Column(
            "status", String(32), nullable=False,
            server_default="pending_activation",
        )
        ddl = _column_ddl("subscriptions", col)
        assert "DEFAULT 'pending_activation'" in ddl

    def test_jsonb_empty_object_default_is_quoted(self):
        """The JSONB "{}" default was a raw syntax error before."""
        col = Column("meta", JSONB, nullable=False, server_default="{}")
        ddl = _column_ddl("wallet_transactions", col)
        assert "DEFAULT '{}'" in ddl

    def test_single_quotes_inside_default_are_escaped(self):
        col = Column("label", String(64), nullable=True, server_default="it's")
        ddl = _column_ddl("t", col)
        assert "DEFAULT 'it''s'" in ddl

    def test_numeric_text_default_untouched(self):
        """text("0") is already SQL — must stay unquoted."""
        col = Column("used_count", Integer, nullable=False, server_default=text("0"))
        ddl = _column_ddl("discount_codes", col)
        assert ddl.endswith("DEFAULT 0")

    def test_boolean_text_default_untouched(self):
        col = Column("is_active", Boolean, nullable=False, server_default=text("true"))
        ddl = _column_ddl("discount_codes", col)
        assert ddl.endswith("DEFAULT true")

    def test_sql_function_default_untouched(self):
        col = Column("created_at", String(64), nullable=True, server_default=text("now()"))
        ddl = _column_ddl("t", col)
        assert ddl.endswith("DEFAULT now()")


# ─── Sections 32 + 59: dashboard plan delete guards ──────────────────────────


@pytest.fixture
def dashboard_admin():
    admin = MagicMock()
    admin.username = "boss"
    return admin


@pytest.fixture
def plan(plan_id):
    p = MagicMock()
    p.id = plan_id
    p.code = "plan_abcd1234"
    p.name = "Test plan"
    return p


def _update_result(codes):
    """Mock result for the DiscountCode bulk-deactivate UPDATE...RETURNING."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = codes
    return result


class TestDeletePlanOrderGuard:
    async def test_plan_with_orders_refused_400(
        self, mock_session, dashboard_admin, plan, plan_id
    ):
        """Orders without subscriptions are normal (failed/refunded buys);
        Order.plan_id is RESTRICT, so the route must refuse cleanly instead
        of 500-ing at commit."""
        # scalar call order: plan lookup, sub count, order count
        mock_session.scalar = AsyncMock(side_effect=[plan, 0, 3])

        with pytest.raises(HTTPException) as exc_info:
            await delete_plan(plan_id, (dashboard_admin, mock_session))

        assert exc_info.value.status_code == 400
        assert "سفارش" in exc_info.value.detail
        mock_session.delete.assert_not_called()
        mock_session.execute.assert_not_called()  # no discount deactivation either
        mock_session.commit.assert_not_called()

    async def test_subscription_guard_still_first(
        self, mock_session, dashboard_admin, plan, plan_id
    ):
        mock_session.scalar = AsyncMock(side_effect=[plan, 2])

        with pytest.raises(HTTPException) as exc_info:
            await delete_plan(plan_id, (dashboard_admin, mock_session))

        assert exc_info.value.status_code == 400
        assert "سرویس" in exc_info.value.detail
        mock_session.delete.assert_not_called()

    async def test_unknown_plan_404(self, mock_session, dashboard_admin):
        mock_session.scalar = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await delete_plan(uuid4(), (dashboard_admin, mock_session))
        assert exc_info.value.status_code == 404


class TestDeletePlanDiscountScopeGuard:
    async def test_plan_scoped_codes_deactivated_before_delete(
        self, mock_session, dashboard_admin, plan, plan_id
    ):
        """SET NULL on DiscountCode.plan_id would silently widen a
        plan-restricted code to ALL plans — the route must flip
        is_active=False on those codes BEFORE deleting the plan."""
        mock_session.scalar = AsyncMock(side_effect=[plan, 0, 0])
        mock_session.execute = AsyncMock(return_value=_update_result(["PROMO10"]))

        result = await delete_plan(plan_id, (dashboard_admin, mock_session))

        assert result["ok"] is True
        mock_session.delete.assert_awaited_once_with(plan)
        mock_session.commit.assert_awaited_once()

        # The single execute() is the discount-deactivation UPDATE.
        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile()
        sql = str(compiled)
        assert "UPDATE discount_codes" in sql
        assert "RETURNING discount_codes.code" in sql
        assert compiled.params["is_active"] is False
        assert compiled.params["plan_id_1"] == plan.id

        # Ordering: codes are deactivated before the plan row is deleted,
        # so the FK SET NULL can never fire on a still-active code.
        call_names = [name for name, _args, _kw in mock_session.mock_calls]
        assert call_names.index("execute") < call_names.index("delete")

    async def test_deactivated_codes_recorded_in_audit_payload(
        self, mock_session, dashboard_admin, plan, plan_id
    ):
        mock_session.scalar = AsyncMock(side_effect=[plan, 0, 0])
        mock_session.execute = AsyncMock(
            return_value=_update_result(["PROMO10", "VIP100"])
        )

        await delete_plan(plan_id, (dashboard_admin, mock_session))

        audit_entry = mock_session.add.call_args.args[0]
        assert audit_entry.payload["deactivated_discount_codes"] == ["PROMO10", "VIP100"]

    async def test_no_scoped_codes_is_a_clean_noop(
        self, mock_session, dashboard_admin, plan, plan_id
    ):
        mock_session.scalar = AsyncMock(side_effect=[plan, 0, 0])
        mock_session.execute = AsyncMock(return_value=_update_result([]))

        result = await delete_plan(plan_id, (dashboard_admin, mock_session))

        assert result["ok"] is True
        mock_session.delete.assert_awaited_once_with(plan)
