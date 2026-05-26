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
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory
from core.dashboard_auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_OPTS,
    SESSION_TTL_SECONDS,
    issue_session,
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
    # Slide the expiry forward on every authenticated hit.
    new_token = issue_session(admin.id)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=new_token,
        max_age=SESSION_TTL_SECONDS,
        **SESSION_COOKIE_OPTS,
    )
    return admin, session
