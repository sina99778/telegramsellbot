"""
Regression tests for finding #11: hard-deleting a server crashed because the
ORM tried to nullify the NOT NULL xui_inbounds.server_id FK.

The fix replaces `session.delete(server)` in the hard-delete branch of
apps/bot/handlers/admin/servers.py:delete_server with explicit bulk DELETE
statements issued in FK order (clients -> inbounds -> credentials -> server),
so the unit-of-work never touches the loaded `inbounds` relationship.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Delete

from apps.bot.handlers.admin.servers import ServerActionCallback, delete_server


@pytest.fixture
def server():
    record = MagicMock()
    record.id = uuid4()
    record.name = "test-server"
    record.is_active = True
    record.health_status = "healthy"
    record.inbounds = [MagicMock(is_active=True), MagicMock(is_active=True)]
    return record


@pytest.fixture
def callback():
    cb = MagicMock()
    cb.answer = AsyncMock()
    return cb


@pytest.fixture
def admin_user():
    user = MagicMock()
    user.id = uuid4()
    return user


def _delete_table_names(session) -> list[str]:
    """Table names of the Delete statements passed to session.execute, in order."""
    names = []
    for call in session.execute.call_args_list:
        stmt = call.args[0]
        if isinstance(stmt, Delete):
            names.append(stmt.table.name)
    return names


async def test_hard_delete_issues_bulk_deletes_in_fk_order(
    mock_session, server, callback, admin_user
):
    """active_client_count == 0 -> explicit child deletes, never session.delete()."""
    # First scalar() loads the server, second returns the active-client count.
    mock_session.scalar = AsyncMock(side_effect=[server, 0])
    callback_data = ServerActionCallback(action="del_ok", server_id=server.id, page=1)

    with patch(
        "apps.bot.handlers.admin.servers.list_servers", new_callable=AsyncMock
    ):
        await delete_server(callback, callback_data, mock_session, admin_user)

    # The buggy path (ORM delete with loaded inbounds -> FK nullify) is gone.
    mock_session.delete.assert_not_called()

    # Children are removed before their parents: clients -> inbounds ->
    # credentials -> server.
    assert _delete_table_names(mock_session) == [
        "xui_clients",
        "xui_inbounds",
        "xui_server_credentials",
        "xui_servers",
    ]


async def test_soft_delete_with_active_clients_does_not_delete_rows(
    mock_session, server, callback, admin_user
):
    """active_client_count > 0 -> archive only: no DELETE statements at all."""
    mock_session.scalar = AsyncMock(side_effect=[server, 3])
    callback_data = ServerActionCallback(action="del_ok", server_id=server.id, page=1)

    with patch(
        "apps.bot.handlers.admin.servers.list_servers", new_callable=AsyncMock
    ):
        await delete_server(callback, callback_data, mock_session, admin_user)

    mock_session.delete.assert_not_called()
    assert _delete_table_names(mock_session) == []

    # Soft-delete semantics stay intact.
    assert server.is_active is False
    assert server.health_status == "deleted"
    assert all(inbound.is_active is False for inbound in server.inbounds)
