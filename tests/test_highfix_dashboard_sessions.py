"""
SEC-002 — dashboard session revocation + absolute max lifetime.

Before this fix a dashboard session cookie was a stateless signed token
with exactly one property: "valid until exp". Two consequences:

  1. Nothing could revoke it. Logout deleted the browser's copy, and a
     password reset or account disable did nothing to tokens already in
     the wild — anyone holding a captured cookie kept full access for the
     rest of its 14 days.

  2. The 14-day window was re-issued on *every* authenticated request, so
     a stolen cookie that keeps being used never expires at all.

The fix threads two extra fields through the token — `issued_at` (an
anchor that survives re-issue) and `session_version` (an auth epoch
mirrored from the admin row) — so the tests below are mostly about those
two values surviving, or failing to survive, the right operations.

Style note: these are direct-call unit tests, not TestClient tests. The
dependency takes `request`/`response`/`session` explicitly, so a
SimpleNamespace request and a real starlette Response give full coverage
of the cookie behaviour without standing up an app or a database.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from core.dashboard_auth import (
    SESSION_ABSOLUTE_MAX_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    _b64e,
    _secret_bytes,
    issue_session,
    remaining_session_seconds,
    verify_session,
)

DAY = 24 * 3600


# ── Helpers ──────────────────────────────────────────────────────────────

def _admin(session_version: int = 0, is_active: bool = True):
    """Stand-in for a DashboardAdmin row.

    The dependency only touches id / is_active / session_version, so a
    namespace is enough and keeps the test independent of the ORM.
    """
    return SimpleNamespace(
        id=uuid4(),
        username="ops",
        display_name=None,
        last_login_at=None,
        is_active=is_active,
        session_version=session_version,
        password_hash="scrypt$16384$8$1$AAAA$AAAA",
    )


def _request(token: str | None):
    cookies = {} if token is None else {SESSION_COOKIE_NAME: token}
    return SimpleNamespace(cookies=cookies)


def _session_for(admin):
    session = AsyncMock()
    session.get = AsyncMock(return_value=admin)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _session_cookie(response: Response):
    """Return the (value, max_age) of the session cookie set on `response`."""
    for key, raw in response.raw_headers:
        if key.decode("latin-1").lower() != "set-cookie":
            continue
        jar = SimpleCookie()
        jar.load(raw.decode("latin-1"))
        morsel = jar.get(SESSION_COOKIE_NAME)
        if morsel is not None:
            max_age = morsel["max-age"]
            return morsel.value, (int(max_age) if max_age not in ("", None) else None)
    return None, None


def _legacy_token(admin_id, exp: int) -> str:
    """A *correctly signed* token in the old 2-field format.

    Signed with the live secret on purpose: if verify_session rejects it,
    the only possible reason is the payload shape, which is exactly the
    property under test.
    """
    payload = f"{admin_id}.{exp}".encode("ascii")
    sig = hmac.new(_secret_bytes(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


# ── Token layer ──────────────────────────────────────────────────────────

def test_token_round_trip_carries_anchor_and_epoch():
    admin_id = uuid4()
    before = int(time.time())
    token = issue_session(admin_id, session_version=7)
    payload = verify_session(token)

    assert payload is not None
    assert payload.admin_id == admin_id
    assert payload.session_version == 7
    assert before <= payload.issued_at <= int(time.time())
    assert payload.expires_at >= payload.issued_at + SESSION_TTL_SECONDS - 2


def test_reissued_token_can_inherit_the_original_anchor():
    admin_id = uuid4()
    anchor = int(time.time()) - 5 * DAY
    payload = verify_session(issue_session(admin_id, issued_at=anchor))

    assert payload is not None
    assert payload.issued_at == anchor  # not reset to "now"


def test_legacy_two_field_token_is_rejected():
    """Fail-closed: a pre-fix token has no anchor and no epoch, so honouring
    it would silently keep the vulnerability alive for 14 more days."""
    token = _legacy_token(uuid4(), int(time.time()) + SESSION_TTL_SECONDS)
    assert verify_session(token) is None


def test_tampered_signature_is_rejected():
    token = issue_session(uuid4())
    payload_b64, sig_b64 = token.split(".", 1)
    forged = f"{payload_b64}.{_b64e(b'x' * 32)}"
    assert forged != token
    assert verify_session(forged) is None


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    from pydantic import SecretStr

    from core.config import settings

    token = issue_session(uuid4())
    monkeypatch.setattr(settings, "app_secret_key", SecretStr("a-different-key"))
    assert verify_session(token) is None


def test_expired_token_is_rejected():
    assert verify_session(issue_session(uuid4(), ttl=-10)) is None


def test_token_past_the_absolute_ceiling_is_rejected_even_though_exp_is_future():
    """The heart of the fix. `exp` is an hour away — perfectly fresh by the
    old rules — but the session started more than 30 days ago, so it dies."""
    stale_anchor = int(time.time()) - SESSION_ABSOLUTE_MAX_SECONDS - 60
    token = issue_session(uuid4(), ttl=3600, issued_at=stale_anchor)
    assert verify_session(token) is None


def test_garbage_tokens_never_raise():
    for bad in ("", ".", "not-a-token", "a.b.c", "$$$.$$$", "x" * 500):
        assert verify_session(bad) is None


# ── remaining_session_seconds ────────────────────────────────────────────

def test_remaining_seconds_is_the_full_window_for_a_fresh_session():
    payload = verify_session(issue_session(uuid4()))
    assert payload is not None
    assert remaining_session_seconds(payload) == SESSION_TTL_SECONDS


def test_remaining_seconds_shrinks_near_the_absolute_deadline():
    now = int(time.time())
    payload = verify_session(issue_session(uuid4(), ttl=3600, issued_at=now - 29 * DAY))
    assert payload is not None

    remaining = remaining_session_seconds(payload, now=now)
    assert remaining == DAY  # one day left of the 30, not another 14
    assert remaining < SESSION_TTL_SECONDS


def test_remaining_seconds_never_goes_negative():
    now = int(time.time())
    payload = verify_session(issue_session(uuid4(), ttl=3600, issued_at=now - 40 * DAY))
    # verify_session already refuses this one, so build the payload directly
    # to prove the arithmetic clamps rather than returning a negative TTL.
    assert payload is None

    from core.dashboard_auth import SessionPayload

    stale = SessionPayload(
        admin_id=uuid4(),
        expires_at=now + 3600,
        issued_at=now - 40 * DAY,
        session_version=0,
    )
    assert remaining_session_seconds(stale, now=now) == 0


# ── require_dashboard_admin ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_session_is_accepted_and_cookie_is_reissued():
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin(session_version=3)
    token = issue_session(admin.id, session_version=3)
    response = Response()

    got_admin, _ = await require_dashboard_admin(
        _request(token), response, _session_for(admin),
    )

    assert got_admin is admin
    value, max_age = _session_cookie(response)
    assert max_age == SESSION_TTL_SECONDS
    # Not asserting value != token: a re-issue inside the same wall-clock
    # second is byte-identical by construction (same admin, anchor, epoch and
    # exp), which is fine. What matters is that the cookie is set and that it
    # still verifies to this admin.
    reissued = verify_session(value)
    assert reissued is not None
    assert reissued.admin_id == admin.id
    assert reissued.session_version == 3


@pytest.mark.asyncio
async def test_reissued_cookie_does_not_extend_the_absolute_deadline():
    """Regression guard for the "immortal session" bug: before the fix the
    dependency called issue_session(admin.id) with no anchor, so every hit
    reset the clock and an active stolen cookie lived forever."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin()
    anchor = int(time.time()) - 20 * DAY
    token = issue_session(admin.id, ttl=3600, issued_at=anchor)
    response = Response()

    await require_dashboard_admin(_request(token), response, _session_for(admin))

    value, _ = _session_cookie(response)
    reissued = verify_session(value)
    assert reissued is not None
    assert reissued.issued_at == anchor  # anchor survived the round trip


@pytest.mark.asyncio
async def test_reissued_cookie_max_age_is_clamped_to_the_deadline():
    """Cookie lifetime and token lifetime must move together — a cookie that
    outlives its token means the browser keeps sending a dead credential and
    the operator gets a hard 401 instead of the login screen."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin()
    token = issue_session(admin.id, ttl=3600, issued_at=int(time.time()) - 29 * DAY)
    response = Response()

    await require_dashboard_admin(_request(token), response, _session_for(admin))

    value, max_age = _session_cookie(response)
    reissued = verify_session(value)
    assert reissued is not None
    assert max_age is not None
    assert 0 < max_age <= DAY
    assert max_age < SESSION_TTL_SECONDS
    # Token expiry and cookie expiry agree to within a second of clock skew.
    assert abs((reissued.expires_at - int(time.time())) - max_age) <= 1


@pytest.mark.asyncio
async def test_missing_cookie_is_rejected():
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(None), Response(), _session_for(_admin()))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_disabled_admin_is_rejected():
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin(is_active=False)
    token = issue_session(admin.id)

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(token), Response(), _session_for(admin))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_deleted_admin_is_rejected():
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    token = issue_session(uuid4())
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(token), Response(), session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_token_from_an_older_epoch_is_revoked():
    """A perfectly valid, unexpired, correctly signed token is refused
    because the admin row's epoch moved on."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin(session_version=5)
    token = issue_session(admin.id, session_version=4)  # minted before the bump

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(token), Response(), _session_for(admin))
    assert exc.value.status_code == 401
    assert exc.value.detail == "session revoked"


@pytest.mark.asyncio
async def test_token_from_a_newer_epoch_is_also_revoked():
    """Strict equality, not >=: a token claiming an epoch the row never
    reached is a forgery attempt or a rolled-back DB, both fail-closed."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin(session_version=1)
    token = issue_session(admin.id, session_version=99)

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(token), Response(), _session_for(admin))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_epoch_check_costs_no_extra_query():
    """The admin row is already loaded for the is_active check, so revocation
    must not add a round trip on the hot path."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin

    admin = _admin()
    session = _session_for(admin)
    await require_dashboard_admin(
        _request(issue_session(admin.id)), Response(), session,
    )
    assert session.get.await_count == 1


# ── POST /logout ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_logout_bumps_the_epoch_and_clears_the_cookie():
    from apps.api.routes.dashboard.auth import logout

    admin = _admin(session_version=2)
    session = _session_for(admin)
    response = Response()

    result = await logout(_request(issue_session(admin.id, session_version=2)), response, session)

    assert result == {"ok": True}
    assert admin.session_version == 3
    session.commit.assert_awaited_once()
    value, _ = _session_cookie(response)
    assert value == ""  # delete_cookie writes an empty value


@pytest.mark.asyncio
async def test_logout_revokes_the_token_end_to_end():
    """The property that actually matters: after logout, a copy of the cookie
    kept by an attacker no longer authenticates."""
    from apps.api.routes.dashboard._deps import require_dashboard_admin
    from apps.api.routes.dashboard.auth import logout

    admin = _admin(session_version=0)
    stolen = issue_session(admin.id, session_version=0)

    # Sanity: it works before logout.
    await require_dashboard_admin(_request(stolen), Response(), _session_for(admin))

    await logout(_request(stolen), Response(), _session_for(admin))

    with pytest.raises(HTTPException) as exc:
        await require_dashboard_admin(_request(stolen), Response(), _session_for(admin))
    assert exc.value.detail == "session revoked"


@pytest.mark.asyncio
async def test_logout_without_a_cookie_is_still_ok():
    from apps.api.routes.dashboard.auth import logout

    session = _session_for(_admin())
    result = await logout(_request(None), Response(), session)

    assert result == {"ok": True}
    session.get.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_with_a_junk_cookie_is_still_ok():
    from apps.api.routes.dashboard.auth import logout

    session = _session_for(_admin())
    result = await logout(_request("not-a-real-token"), Response(), session)

    assert result == {"ok": True}
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_survives_a_database_failure():
    """Sign-out is a gesture the user must always be able to complete; a DB
    blip degrades to "at least drop the cookie" rather than a 500."""
    from apps.api.routes.dashboard.auth import logout

    admin = _admin()
    session = _session_for(admin)
    session.get = AsyncMock(side_effect=RuntimeError("db down"))
    response = Response()

    result = await logout(_request(issue_session(admin.id)), response, session)

    assert result == {"ok": True}
    session.rollback.assert_awaited_once()
    value, _ = _session_cookie(response)
    assert value == ""


# ── POST /login ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_stamps_the_current_epoch_into_the_token():
    from apps.api.routes.dashboard.auth import LoginIn, login

    admin = _admin(session_version=11)
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=admin)
    session.commit = AsyncMock()
    response = Response()

    with patch("apps.api.routes.dashboard.auth.verify_password", return_value=True), \
         patch("apps.api.routes.dashboard.auth.needs_rehash", return_value=False):
        result = await login(
            LoginIn(username="sec002-login", password="whatever"),
            response,
            session,
        )

    assert result["ok"] is True
    value, max_age = _session_cookie(response)
    payload = verify_session(value)
    assert payload is not None
    assert payload.session_version == 11
    assert max_age == SESSION_TTL_SECONDS
    # A brand-new session anchors on now, so it gets the full 30 days.
    assert remaining_session_seconds(payload) == SESSION_TTL_SECONDS
