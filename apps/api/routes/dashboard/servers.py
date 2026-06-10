"""
Dashboard server-management endpoints:

    GET    /api/dashboard/servers                       — list + summary stats
    GET    /api/dashboard/servers/{id}                  — detail + inbounds
    POST   /api/dashboard/servers                       — create
    PATCH  /api/dashboard/servers/{id}                  — edit
    DELETE /api/dashboard/servers/{id}                  — delete (safety-gated)
    POST   /api/dashboard/servers/{id}/test             — test the panel login

Encryption note: X-UI passwords are stored Fernet-encrypted in
`xui_server_credentials.password_encrypted`. The encrypt/decrypt
helpers in `core/security.py` use APP_SECRET_KEY as the Fernet key,
which is why every server-migration story (Phase 4 of the operator
bundle) MUST carry the same APP_SECRET_KEY across hosts.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.routes.dashboard._deps import require_dashboard_admin
from core.security import decrypt_secret, encrypt_secret
from services.panels.adapter import is_marzban_family
from models.dashboard_admin import DashboardAdmin
from models.subscription import Subscription
from models.xui import (
    XUIClientRecord,
    XUIInboundRecord,
    XUIServerCredential,
    XUIServerRecord,
)
from repositories.audit import AuditLogRepository


logger = logging.getLogger(__name__)
router = APIRouter()


AuthDep = Annotated[
    tuple[DashboardAdmin, AsyncSession],
    Depends(require_dashboard_admin),
]


# ─── List + summary ──────────────────────────────────────────────────────


@router.get("")
async def list_servers(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    servers = (await session.execute(
        select(XUIServerRecord)
        .order_by(XUIServerRecord.priority.asc(), XUIServerRecord.name.asc())
    )).scalars().all()

    items: list[dict[str, Any]] = []
    for s in servers:
        # Cheap-ish per-server stats. Two scalar queries per server is
        # fine for the ~10-50 servers operators typically run.
        inbound_count = int(await session.scalar(
            select(func.count(XUIInboundRecord.id))
            .where(XUIInboundRecord.server_id == s.id)
        ) or 0)
        active_inbound_count = int(await session.scalar(
            select(func.count(XUIInboundRecord.id))
            .where(
                XUIInboundRecord.server_id == s.id,
                XUIInboundRecord.is_active.is_(True),
            )
        ) or 0)
        client_count = int(await session.scalar(
            select(func.count(XUIClientRecord.id))
            .join(XUIInboundRecord, XUIInboundRecord.id == XUIClientRecord.inbound_id)
            .where(XUIInboundRecord.server_id == s.id)
        ) or 0)

        items.append({
            "id": str(s.id),
            "name": s.name,
            "base_url": s.base_url,
            "panel_type": s.panel_type,
            "is_active": s.is_active,
            "priority": s.priority,
            "health_status": s.health_status,
            "subscription_port": s.subscription_port,
            "config_domain": s.config_domain,
            "sub_domain": s.sub_domain,
            "max_clients": s.max_clients,
            "inbound_count": inbound_count,
            "active_inbound_count": active_inbound_count,
            "client_count": client_count,
        })

    return {"items": items, "total": len(items)}


# ─── Detail ──────────────────────────────────────────────────────────────


@router.get("/{server_id}")
async def server_detail(server_id: UUID, auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth

    s = await session.scalar(
        select(XUIServerRecord)
        .options(
            selectinload(XUIServerRecord.inbounds),
            selectinload(XUIServerRecord.credentials),
        )
        .where(XUIServerRecord.id == server_id)
    )
    if s is None:
        raise HTTPException(status_code=404, detail="سرور یافت نشد.")

    inbounds_payload: list[dict[str, Any]] = []
    for ib in s.inbounds:
        client_count = int(await session.scalar(
            select(func.count(XUIClientRecord.id))
            .where(XUIClientRecord.inbound_id == ib.id)
        ) or 0)
        inbounds_payload.append({
            "id": str(ib.id),
            "xui_inbound_remote_id": int(ib.xui_inbound_remote_id),
            "remark": ib.remark,
            "protocol": ib.protocol,
            "port": ib.port,
            "tag": ib.tag,
            "is_active": ib.is_active,
            "client_count": client_count,
        })

    return {
        "server": {
            "id": str(s.id),
            "name": s.name,
            "base_url": s.base_url,
            "panel_type": s.panel_type,
            "is_active": s.is_active,
            "priority": s.priority,
            "health_status": s.health_status,
            "subscription_port": s.subscription_port,
            "config_domain": s.config_domain,
            "sub_domain": s.sub_domain,
            "max_clients": s.max_clients,
            "credentials_username": s.credentials.username if s.credentials else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        },
        "inbounds": inbounds_payload,
    }


# ─── Create ──────────────────────────────────────────────────────────────


class ServerCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field(..., min_length=4, max_length=1024)
    panel_username: str = Field(..., min_length=1, max_length=128)
    panel_password: str = Field(..., min_length=1, max_length=256)
    # Any REGISTERED panel kind (default "sanaei_xui"). Validated against the
    # panel registry so a newly-registered panel (e.g. Rebka) auto-enables here
    # without editing this regex — and an unknown kind is rejected (never stored
    # as a server that would be silently driven with the wrong client).
    panel_type: str = Field("sanaei_xui")

    @field_validator("panel_type")
    @classmethod
    def _panel_type_must_be_registered(cls, v: str) -> str:
        from services.panels.registry import is_known_panel_type, known_panel_types
        if not is_known_panel_type(v):
            raise ValueError(f"نوعِ پنل نامعتبر است. مجاز: {sorted(known_panel_types())}")
        return v
    config_domain: str | None = Field(None, max_length=255)
    sub_domain: str | None = Field(None, max_length=255)
    subscription_port: int = Field(2096, ge=1, le=65535)
    max_clients: int | None = Field(None, ge=0)
    priority: int = Field(100, ge=0, le=10000)


@router.post("")
async def create_server(body: ServerCreateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth

    # Name + base_url uniqueness checks (the model enforces name unique,
    # base_url isn't unique by schema but a duplicate is almost always
    # operator error so we surface it as a 400).
    exists = await session.scalar(
        select(XUIServerRecord.id).where(XUIServerRecord.name == body.name)
    )
    if exists is not None:
        raise HTTPException(status_code=400, detail="نام سرور تکراری است.")

    server = XUIServerRecord(
        name=body.name,
        base_url=body.base_url,
        panel_type=body.panel_type,
        is_active=True,
        priority=body.priority,
        subscription_port=body.subscription_port,
        config_domain=body.config_domain,
        sub_domain=body.sub_domain,
        max_clients=body.max_clients,
        health_status="unknown",
    )
    session.add(server)
    await session.flush()

    cred = XUIServerCredential(
        server_id=server.id,
        username=body.panel_username,
        password_encrypted=encrypt_secret(body.panel_password),
    )
    session.add(cred)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_server_create",
            entity_type="xui_server",
            entity_id=server.id,
            payload={"dashboard_admin": admin.username, "name": server.name, "base_url": server.base_url},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True, "id": str(server.id)}


# ─── Edit ────────────────────────────────────────────────────────────────


class ServerUpdateBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    base_url: str | None = Field(None, min_length=4, max_length=1024)
    panel_username: str | None = Field(None, min_length=1, max_length=128)
    panel_password: str | None = Field(None, min_length=1, max_length=256)
    is_active: bool | None = None
    config_domain: str | None = Field(None, max_length=255)
    sub_domain: str | None = Field(None, max_length=255)
    subscription_port: int | None = Field(None, ge=1, le=65535)
    max_clients: int | None = Field(None, ge=0)
    priority: int | None = Field(None, ge=0, le=10000)


@router.patch("/{server_id}")
async def update_server(server_id: UUID, body: ServerUpdateBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    server = await session.scalar(
        select(XUIServerRecord).options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == server_id)
    )
    if server is None:
        raise HTTPException(status_code=404, detail="سرور یافت نشد.")

    changes: dict[str, Any] = {}
    if body.name is not None and body.name != server.name:
        # Uniqueness check.
        dupe = await session.scalar(
            select(XUIServerRecord.id).where(
                XUIServerRecord.name == body.name,
                XUIServerRecord.id != server.id,
            )
        )
        if dupe is not None:
            raise HTTPException(status_code=400, detail="نام سرور تکراری است.")
        server.name = body.name; changes["name"] = body.name
    if body.base_url is not None: server.base_url = body.base_url; changes["base_url"] = body.base_url
    if body.is_active is not None: server.is_active = body.is_active; changes["is_active"] = body.is_active
    if body.config_domain is not None: server.config_domain = body.config_domain
    if body.sub_domain is not None: server.sub_domain = body.sub_domain
    if body.subscription_port is not None: server.subscription_port = body.subscription_port
    if body.max_clients is not None: server.max_clients = body.max_clients
    if body.priority is not None: server.priority = body.priority

    if (body.panel_username is not None) or (body.panel_password is not None):
        cred = server.credentials
        if cred is None:
            if body.panel_username is None or body.panel_password is None:
                raise HTTPException(status_code=400, detail="برای ایجاد credentials هم نام کاربری و هم رمز لازم است.")
            cred = XUIServerCredential(
                server_id=server.id,
                username=body.panel_username,
                password_encrypted=encrypt_secret(body.panel_password),
            )
            session.add(cred)
        else:
            if body.panel_username is not None: cred.username = body.panel_username
            if body.panel_password is not None: cred.password_encrypted = encrypt_secret(body.panel_password)
        changes["credentials"] = "updated"

    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_server_update",
            entity_type="xui_server",
            entity_id=server.id,
            payload={"dashboard_admin": admin.username, "changes": list(changes.keys())},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


# ─── Delete ──────────────────────────────────────────────────────────────


@router.delete("/{server_id}")
async def delete_server(server_id: UUID, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    server = await session.scalar(select(XUIServerRecord).where(XUIServerRecord.id == server_id))
    if server is None:
        raise HTTPException(status_code=404, detail="سرور یافت نشد.")

    # Safety gate: refuse if there are X-UI client rows pointing at any
    # of this server's inbounds. Operator must migrate them off first.
    client_count = int(await session.scalar(
        select(func.count(XUIClientRecord.id))
        .join(XUIInboundRecord, XUIInboundRecord.id == XUIClientRecord.inbound_id)
        .where(XUIInboundRecord.server_id == server_id)
    ) or 0)
    if client_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"این سرور {client_count} کلاینت فعال دارد. ابتدا آن‌ها را منتقل کن، سپس حذف کن.",
        )

    await session.delete(server)
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_server_delete",
            entity_type="xui_server",
            entity_id=server.id,
            payload={"dashboard_admin": admin.username, "name": server.name},
        )
    except Exception as exc:
        logger.warning("Audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


# ─── Test ────────────────────────────────────────────────────────────────


@router.post("/{server_id}/test")
async def test_connection(server_id: UUID, auth: AuthDep) -> dict[str, Any]:
    """Probe the X-UI panel with the stored credentials.

    On success: writes server.health_status="ok" and returns inbound count.
    On failure:  writes server.health_status="error" and returns the
    error message verbatim so the operator can fix the cause.
    """
    _admin, session = auth
    server = await session.scalar(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.id == server_id)
    )
    if server is None:
        raise HTTPException(status_code=404, detail="سرور یافت نشد.")
    if server.credentials is None:
        raise HTTPException(status_code=400, detail="رمز ورود به پنل ثبت نشده است.")

    try:
        password = decrypt_secret(server.credentials.password_encrypted)
    except Exception as exc:
        # APP_SECRET_KEY mismatch — the classic post-migration failure
        # if the operator didn't carry .env across hosts.
        raise HTTPException(
            status_code=500,
            detail=f"خطا در رمزگشایی credentials — APP_SECRET_KEY اشتباه است؟ ({exc})",
        )

    try:
        from pydantic import SecretStr
        from core.config import settings as _settings
        if is_marzban_family(server):
            from services.panels.marzban import marzban_client_from_credentials
            async with marzban_client_from_credentials(
                server.panel_type,
                base_url=server.base_url,
                username=server.credentials.username,
                password=password,
            ) as client:
                await client.login()
                remote = await client.list_bundles()
        else:
            from services.xui.client import SanaeiXUIClient, XUIClientConfig
            config = XUIClientConfig(
                base_url=server.base_url,
                username=server.credentials.username,
                password=SecretStr(password),
                verify_ssl=_settings.xui_verify_ssl,
            )
            async with SanaeiXUIClient(config) as client:
                # NOTE: was a latent bug — client.list_inbounds() doesn't exist;
                # the method is get_inbounds(). Fixed here.
                remote = await client.get_inbounds()
    except Exception as exc:
        server.health_status = "error"
        await session.commit()
        return {"ok": False, "error": str(exc)[:300]}

    server.health_status = "ok"
    await session.commit()
    return {"ok": True, "inbound_count": len(remote)}
