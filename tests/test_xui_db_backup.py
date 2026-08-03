"""X-UI panel DB backup download tests.

The reported bug: the panel DB was missing from the backup archive.

Root cause: 3x-ui mounts getDb under the /panel/api group, whose
`checkAPIAuth` middleware aborts with **404** (not 401) when the session
cookie is missing — deliberately, to hide endpoints from unauthenticated
callers. The client only re-logged-in on 401/403, so an expired cookie made
every candidate endpoint "fail" and the DB was silently dropped.
"""

import httpx
import pytest
from pydantic import SecretStr

from services.xui.client import SanaeiXUIClient, XUIClientConfig, XUIRequestError

DB_BLOB = b"SQLite format 3\x00" + b"x" * 4096
GETDB = "panel/api/server/getDb"


def _config() -> XUIClientConfig:
    return XUIClientConfig(
        base_url="http://panel.example.com:2053",
        username="admin",
        password=SecretStr("secret"),
    )


def _client(handler) -> SanaeiXUIClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(
        transport=transport,
        base_url="http://panel.example.com:2053/",
    )
    return SanaeiXUIClient(_config(), http_client=http_client)


def _login_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "msg": "ok"},
        headers={"set-cookie": "3x-ui=session-token; Path=/"},
    )


@pytest.mark.asyncio
async def test_expired_session_404_triggers_relogin_and_returns_db():
    """The actual reported failure: cookie expired, panel answers 404."""
    calls: list[str] = []
    logged_in = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        calls.append(path)
        if path == "login":
            logged_in["value"] = True
            return _login_response()
        if path == GETDB:
            if not logged_in["value"]:
                return httpx.Response(404)
            return httpx.Response(200, content=DB_BLOB)
        return httpx.Response(404)

    client = _client(handler)
    client._authenticated = True  # stale cookie, as in a pooled client

    blob = await client.get_db_backup()

    assert blob == DB_BLOB
    assert "login" in calls, "expected a re-login after the 404"
    assert calls.count(GETDB) == 2, "expected getDb retried after re-login"


@pytest.mark.asyncio
async def test_401_still_triggers_relogin():
    """Regression guard: the pre-existing 401 path must keep working."""
    logged_in = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path == "login":
            logged_in["value"] = True
            return _login_response()
        if path == GETDB and logged_in["value"]:
            return httpx.Response(200, content=DB_BLOB)
        return httpx.Response(401)

    client = _client(handler)
    client._authenticated = True

    assert await client.get_db_backup() == DB_BLOB


@pytest.mark.asyncio
async def test_correct_3xui_endpoint_is_tried_first():
    """panel/api/server/getDb is where 3x-ui mounts ServerController."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        calls.append(path)
        if path == "login":
            return _login_response()
        if path == GETDB:
            return httpx.Response(200, content=DB_BLOB)
        return httpx.Response(404)

    client = _client(handler)
    client._authenticated = True

    await client.get_db_backup()

    assert calls[0] == GETDB
    # No time wasted on legacy paths when the modern one works.
    assert "panel/setting/getDb" not in calls


@pytest.mark.asyncio
async def test_falls_back_to_legacy_endpoint_on_older_panels():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path == "login":
            return _login_response()
        if path == "panel/setting/getDb":
            return httpx.Response(200, content=DB_BLOB)
        return httpx.Response(404)

    client = _client(handler)
    client._authenticated = True

    assert await client.get_db_backup() == DB_BLOB


@pytest.mark.asyncio
async def test_html_login_page_with_200_is_rejected():
    """A 200 carrying a login page must not be mistaken for a DB."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path == "login":
            return _login_response()
        return httpx.Response(200, content=b"<html><body>login</body></html>")

    client = _client(handler)
    client._authenticated = True

    with pytest.raises(XUIRequestError):
        await client.get_db_backup()


@pytest.mark.asyncio
async def test_error_lists_every_attempted_endpoint():
    """The old message reported only the last endpoint, hiding the real cause."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path == "login":
            return _login_response()
        return httpx.Response(500)

    client = _client(handler)
    client._authenticated = True

    with pytest.raises(XUIRequestError) as excinfo:
        await client.get_db_backup()

    message = str(excinfo.value)
    assert GETDB in message
    assert "500" in message
