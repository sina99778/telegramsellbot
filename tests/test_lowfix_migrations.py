"""
Regression tests for low findings #80 and #84 (deploy/migrations).

#80 — root migrations/*.sql (incl. 012 money-safety CHECK constraints) are
never executed by any deploy path. #84 — the webhook-replay partial unique
index on payments (011) likewise lives only in a never-executed SQL file.

Fix: scripts/migrations/005_money_constraints_and_payment_unique.py mirrors
011 + 012 as an idempotent Python migration that deploy.sh's existing
`scripts/migrations/*.py` loop picks up automatically.

These tests are mock-based (no DB): they verify (a) the script is placed so
the deploy loop runs it in order, (b) its statements mirror the .sql files
exactly, and (c) its idempotency / never-block-the-deploy behaviour.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT / "scripts" / "migrations"
    / "005_money_constraints_and_payment_unique.py"
)
SQL_012 = REPO_ROOT / "migrations" / "012_money_and_inventory_constraints.sql"
SQL_011 = REPO_ROOT / "migrations" / "011_restore_provider_payment_unique.sql"


def _load_module():
    spec = importlib.util.spec_from_file_location("mig005", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mig005():
    return _load_module()


# ---------------------------------------------------------------------------
# Fake DB plumbing (no network / no Postgres)
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, db: "FakeDB"):
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "pg_constraint" in sql:
            return 1 if params.get("n") in self._db.existing_constraints else None
        if "pg_indexes" in sql:
            return 1 if self._db.index_exists else None
        raise AssertionError(f"unexpected scalar(): {sql}")

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self._db.executed.append(sql)
        if sql.lstrip().startswith("ALTER TABLE"):
            name = re.search(r"ADD CONSTRAINT (\w+)", sql).group(1)
            if name in self._db.failing_constraints:
                raise RuntimeError(f"rows violate check constraint {name}")
            return MagicMock()
        if "HAVING COUNT(*) > 1" in sql:
            result = MagicMock()
            result.all.return_value = [(d, 2) for d in self._db.duplicates]
            return result
        return MagicMock()

    async def commit(self):
        self._db.commits += 1


class FakeDB:
    def __init__(
        self,
        existing_constraints=(),
        index_exists=False,
        duplicates=(),
        failing_constraints=(),
    ):
        self.existing_constraints = set(existing_constraints)
        self.index_exists = index_exists
        self.duplicates = list(duplicates)
        self.failing_constraints = set(failing_constraints)
        self.executed: list[str] = []
        self.commits = 0

    def factory(self):
        return _FakeSession(self)


def _patch_db(monkeypatch, mig005, db: FakeDB) -> None:
    monkeypatch.setattr(mig005, "AsyncSessionFactory", db.factory)


# ---------------------------------------------------------------------------
# (a) deploy wiring: the script lives where deploy.sh's loop picks it up
# ---------------------------------------------------------------------------

class TestDeployWiring:
    def test_script_exists_in_deploy_glob_dir(self):
        assert SCRIPT_PATH.is_file()
        deploy = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
        assert "scripts/migrations/*.py" in deploy

    def test_script_sorts_before_catch_all_999(self):
        # deploy.sh relies on bash glob expansion order (lexicographic):
        # explicit migrations must run before 999_auto_sync_columns.
        names = sorted(
            p.name for p in (REPO_ROOT / "scripts" / "migrations").glob("*.py")
        )
        assert SCRIPT_PATH.name in names
        assert names.index(SCRIPT_PATH.name) < names.index("999_auto_sync_columns.py")


# ---------------------------------------------------------------------------
# (b) the script mirrors the .sql sources exactly (drift guard)
# ---------------------------------------------------------------------------

class TestMirrorsSqlSources:
    def test_check_constraints_match_012_sql(self, mig005):
        sql = SQL_012.read_text(encoding="utf-8")
        sql_pairs = set(
            re.findall(r"ALTER TABLE\s+(\w+)\s+ADD CONSTRAINT\s+(\w+)", sql)
        )
        module_pairs = {(t, n) for n, t, _ in mig005._CHECK_CONSTRAINTS}
        assert module_pairs == sql_pairs
        assert len(mig005._CHECK_CONSTRAINTS) == 12

    def test_each_012_constraint_has_pg_constraint_guard_in_sql(self):
        # The .sql file itself guards every constraint — the Python mirror
        # must cover the same set of names.
        sql = SQL_012.read_text(encoding="utf-8")
        guarded = set(re.findall(r"conname = '(\w+)'", sql))
        added = set(re.findall(r"ADD CONSTRAINT\s+(\w+)", sql))
        assert guarded == added

    def test_payment_index_matches_011_sql(self, mig005):
        sql = SQL_011.read_text(encoding="utf-8")
        assert mig005._INDEX_NAME in sql
        assert mig005._INDEX_NAME == "uq_payments_provider_payment_id_automated"
        # Same predicate: NOT NULL + the three automated providers.
        assert "provider_payment_id IS NOT NULL" in mig005._INDEX_PREDICATE
        for provider in ("nowpayments", "tetrapay", "tronado"):
            assert f"'{provider}'" in mig005._INDEX_PREDICATE
            assert f"'{provider}'" in sql


# ---------------------------------------------------------------------------
# (c) idempotency + never-block-the-deploy behaviour
# ---------------------------------------------------------------------------

class TestCheckConstraints:
    async def test_fresh_db_adds_all_constraints(self, mig005, monkeypatch):
        db = FakeDB()
        _patch_db(monkeypatch, mig005, db)
        created, present, warnings = await mig005._apply_check_constraints()
        assert created == 12
        assert present == 0
        assert warnings == []
        alters = [s for s in db.executed if s.lstrip().startswith("ALTER TABLE")]
        assert len(alters) == 12
        assert db.commits == 12  # one commit per constraint (isolation)

    async def test_rerun_is_noop(self, mig005, monkeypatch):
        all_names = [n for n, _, _ in mig005._CHECK_CONSTRAINTS]
        db = FakeDB(existing_constraints=all_names)
        _patch_db(monkeypatch, mig005, db)
        created, present, warnings = await mig005._apply_check_constraints()
        assert created == 0
        assert present == 12
        assert warnings == []
        assert db.executed == []  # no ALTER even attempted

    async def test_one_violating_table_does_not_block_the_rest(
        self, mig005, monkeypatch
    ):
        db = FakeDB(failing_constraints={"ck_wallet_tx_amount_positive"})
        _patch_db(monkeypatch, mig005, db)
        created, present, warnings = await mig005._apply_check_constraints()
        assert created == 11
        assert len(warnings) == 1
        assert "ck_wallet_tx_amount_positive" in warnings[0]


class TestPaymentUniqueIndex:
    async def test_clean_db_creates_partial_unique_index(self, mig005, monkeypatch):
        db = FakeDB()
        _patch_db(monkeypatch, mig005, db)
        status, dups = await mig005._apply_payment_unique_index()
        assert status == "created"
        assert dups == []
        create = next(s for s in db.executed if "CREATE UNIQUE INDEX" in s)
        assert "IF NOT EXISTS" in create  # idempotent DDL
        assert mig005._INDEX_NAME in create
        assert "ON payments (provider, provider_payment_id)" in create
        assert "WHERE" in create  # partial — manual rows stay duplicable
        assert db.commits == 1

    async def test_existing_index_is_noop(self, mig005, monkeypatch):
        db = FakeDB(index_exists=True)
        _patch_db(monkeypatch, mig005, db)
        status, dups = await mig005._apply_payment_unique_index()
        assert status == "exists"
        assert db.executed == []
        assert db.commits == 0

    async def test_duplicates_skip_creation_without_deleting(
        self, mig005, monkeypatch
    ):
        db = FakeDB(duplicates=["nowpayments/abc123"])
        _patch_db(monkeypatch, mig005, db)
        status, dups = await mig005._apply_payment_unique_index()
        assert status == "duplicates"
        assert dups == ["nowpayments/abc123"]
        assert not any("CREATE UNIQUE INDEX" in s for s in db.executed)
        assert not any("DELETE" in s.upper() for s in db.executed)


class TestMainExitCodes:
    def test_main_exits_zero_on_clean_run(self, mig005, monkeypatch):
        _patch_db(monkeypatch, mig005, FakeDB())
        assert mig005.main() == 0

    def test_main_exits_zero_on_data_conflicts(self, mig005, monkeypatch):
        # Duplicates + a violating table must NOT block the deploy.
        db = FakeDB(
            duplicates=["tetrapay/dup1"],
            failing_constraints={"ck_discount_used_not_exceed_max"},
        )
        _patch_db(monkeypatch, mig005, db)
        assert mig005.main() == 0

    def test_main_exits_one_on_infrastructure_failure(self, mig005, monkeypatch):
        def _broken_factory():
            raise ConnectionError("db unreachable")

        monkeypatch.setattr(mig005, "AsyncSessionFactory", _broken_factory)
        assert mig005.main() == 1
