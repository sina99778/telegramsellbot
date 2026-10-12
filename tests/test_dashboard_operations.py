"""Operations cards must count full queues and avoid per-server N+1 queries."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from apps.api.routes.dashboard.overview import get_operations, router
from apps.api.routes.dashboard._deps import require_dashboard_admin


async def test_operations_counts_are_not_clipped_and_server_ids_serialize():
    sid = uuid4()
    counts = MagicMock()
    counts.mappings.return_value.one.return_value = {"pending_receipts": 601, "pending_payments": 1503}
    servers = MagicMock()
    servers.mappings.return_value.all.return_value = [{
        "id": sid, "name": "test", "panel_type": "sanaei_xui", "is_active": True,
        "health_status": "healthy", "max_clients": 20, "inbound_count": 2,
        "active_inbound_count": 1, "client_count": 15, "active_client_count": 12,
    }]
    session = AsyncMock()
    session.execute.side_effect = [counts, servers]
    result = await get_operations((MagicMock(), session))
    assert result["pending_receipts"] == 601
    assert result["pending_payments"] == 1503
    assert result["servers"][0]["id"] == str(sid)
    assert result["servers"][0]["active_client_count"] == 12
    assert session.execute.await_count == 2
    sql = [str(c.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})) for c in session.execute.await_args_list]
    assert "LIMIT" not in sql[0]
    assert "pending_approval" in sql[0] and "card_to_card" in sql[0]
    assert "waiting_receipt" in sql[0] and "manual_crypto" in sql[0]
    assert "GROUP BY" in sql[1] and "LEFT OUTER JOIN" in sql[1]
    # DISTINCT protects inbound counts from multiplication by client rows.
    assert sql[1].count("distinct") >= 2


async def test_empty_operations_queues_are_zero_not_unknown():
    counts = MagicMock()
    counts.mappings.return_value.one.return_value = {"pending_receipts": 0, "pending_payments": 0}
    servers = MagicMock()
    servers.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.side_effect = [counts, servers]
    result = await get_operations((MagicMock(), session))
    assert result["pending_receipts"] == result["pending_payments"] == 0
    assert result["servers"] == []


def test_operations_route_remains_protected_by_admin_dependency():
    app = FastAPI()
    app.include_router(router, prefix="/overview")
    from fastapi import HTTPException
    async def denied():
        raise HTTPException(status_code=401, detail="Unauthorized")
    app.dependency_overrides[require_dashboard_admin] = denied
    with TestClient(app) as client:
        assert client.get("/overview/operations").status_code == 401


async def test_receipt_search_and_pagination_do_not_hide_older_records():
    from apps.api.routes.dashboard.receipts import list_pending_receipts
    session = AsyncMock()
    session.scalar.return_value = 601
    result = MagicMock()
    result.scalars.return_value.all.return_value = [MagicMock()]
    session.execute.return_value = result
    with patch("apps.api.routes.dashboard.receipts._serialize", return_value={"id": "fixture"}):
        response = await list_pending_receipts((MagicMock(), session), page=3, page_size=50, q="test_user%")
    assert response["total"] == 601 and response["total_pages"] == 13
    assert response["page"] == 3 and len(response["items"]) == 1
    count_sql = str(session.scalar.await_args.args[0])
    rows_sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "LIMIT" not in count_sql
    assert "LIMIT 50 OFFSET 100" in rows_sql
    params = session.execute.await_args.args[0].compile().params
    assert "%test\\_user\\%%" in params.values()


async def test_pending_queue_can_reach_rows_after_first_hundred():
    from apps.api.routes.dashboard.transactions import list_pending_approvals
    session = AsyncMock()
    session.scalar.return_value = 503
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    response = await list_pending_approvals((MagicMock(), session), page=6, page_size=25)
    assert response["total"] == 503 and response["total_pages"] == 21
    rows_sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "LIMIT 25 OFFSET 125" in rows_sql
    assert "LIMIT" not in str(session.scalar.await_args.args[0])


def test_receipts_reject_invalid_page_without_executing_query():
    from apps.api.routes.dashboard.receipts import router as receipts_router
    session = AsyncMock()
    app = FastAPI()
    app.include_router(receipts_router, prefix="/receipts")
    async def authorized():
        return MagicMock(), session
    app.dependency_overrides[require_dashboard_admin] = authorized
    with TestClient(app) as client:
        assert client.get("/receipts?page=0").status_code == 422
        assert client.get("/receipts?page_size=100000").status_code == 422
    session.execute.assert_not_awaited()
