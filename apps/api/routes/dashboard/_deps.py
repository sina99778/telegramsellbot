"""
Dashboard-side request dependencies.

`require_dashboard_admin` reads the session cookie, validates it, loads
the matching DashboardAdmin row, and rejects with 401 if anything
about it is off (signature, expiry, soft-disabled, deleted, …).

Use as:

    @router.get("/foo")
    async def foo(admin: tuple[DashboardAdmin, AsyncSession] = Depends(require_dashboard_admin)):
        ...

The session cookie is RE-ISSUED on every successful request so an
active admin's expiry slides forward — they don't get logged out
mid-task just because 14 days passed in calendar time.

Two things keep that sliding window honest:

  * the re-issued token inherits the original `issued_at`, so the
    30-day absolute ceiling still applies; and

  * the token's `session_version` is compared against the admin row on
    every hit, so bumping that column (logout, password reset, disable)
    invalidates outstanding cookies immediately.

Both checks are free: the admin row is already being loaded for the
`is_active` check, so revocation adds no queries.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory
from core.dashboard_auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_OPTS,
    issue_session,
    remaining_session_seconds,
    verify_session,
)
from models.dashboard_admin import DashboardAdmin


async def _open_session() -> AsyncSession:
    async with AsyncSessionFactory() as s:
        yield s


SessionDep = Annotated[AsyncSession, Depends(_open_session)]


async def require_dashboard_admin(
    request: Request,
    response: Response,
    session: SessionDep,
) -> tuple[DashboardAdmin, AsyncSession]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session(token or "")
    if payload is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    admin = await session.get(DashboardAdmin, payload.admin_id)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="admin not found or disabled")
    # Revocation check. A bumped epoch on the row means every token minted
    # before the bump is dead, regardless of its signature or expiry.
    current_version = int(admin.session_version or 0)
    if int(payload.session_version) != current_version:
        raise HTTPException(status_code=401, detail="session revoked")
    # Slide the expiry forward on every authenticated hit, but never past the
    # absolute deadline anchored at the original login.
    ttl = remaining_session_seconds(payload)
    if ttl <= 0:
        raise HTTPException(status_code=401, detail="session expired")
    new_token = issue_session(
        admin.id,
        ttl=ttl,
        session_version=current_version,
        issued_at=payload.issued_at,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        # Same value as the token TTL on purpose. If the cookie outlived the
        # token the browser would keep sending a dead credential and the
        # operator would see a hard 401 instead of the login screen.
        max_age=ttl,
        **SESSION_COOKIE_OPTS,
    )
    return admin, session
