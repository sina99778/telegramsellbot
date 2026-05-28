"""
Dashboard endpoints for brand identity + bot text templates.

    GET    /api/dashboard/brand                — current brand settings
    PATCH  /api/dashboard/brand                — update name / logo / accent / support

    GET    /api/dashboard/text_templates       — catalogue + current overrides
    PATCH  /api/dashboard/text_templates       — merge new overrides

Brand settings also feed the public miniapp endpoint
`/api/miniapp/brand` so the operator's color + logo render in the
mini-app without a deploy.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import require_dashboard_admin
from models.dashboard_admin import DashboardAdmin
from repositories.audit import AuditLogRepository
from repositories.settings import AppSettingsRepository
from services.text_templates import (
    catalogue_dict,
    clear_text_template_cache,
)


logger = logging.getLogger(__name__)
router = APIRouter()
AuthDep = Annotated[tuple[DashboardAdmin, AsyncSession], Depends(require_dashboard_admin)]


# ─── Brand ─────────────────────────────────────────────────────────


@router.get("/brand")
async def get_brand(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    b = await AppSettingsRepository(session).get_brand_settings()
    return {
        "name": b.name,
        "logo_url": b.logo_url,
        "accent_color": b.accent_color,
        "support_handle": b.support_handle,
    }


class BrandBody(BaseModel):
    name: str | None = Field(None, max_length=64)
    logo_url: str | None = Field(None, max_length=512)
    accent_color: str | None = Field(None, pattern=r"^(#[0-9a-fA-F]{6}|)$")
    support_handle: str | None = Field(None, max_length=64)


@router.patch("/brand")
async def patch_brand(body: BrandBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    kwargs: dict[str, Any] = {}
    for f in ("name", "logo_url", "accent_color", "support_handle"):
        v = getattr(body, f)
        if v is not None:
            kwargs[f] = v
    if not kwargs:
        return {"ok": True}
    try:
        await repo.update_brand_settings(**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_brand_update",
            entity_type="app_settings",
            entity_id=admin.id,
            payload={"dashboard_admin": admin.username, **kwargs},
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}


# ─── Text templates ────────────────────────────────────────────────


@router.get("/text_templates")
async def get_text_templates(auth: AuthDep) -> dict[str, Any]:
    _admin, session = auth
    overrides = await AppSettingsRepository(session).get_all_text_templates()
    return {
        "catalogue": catalogue_dict(),
        "overrides": overrides,
    }


class TextTemplatesBody(BaseModel):
    # null value → clear that override (fall back to code default).
    # Any keys not present → untouched.
    templates: dict[str, str | None] = Field(default_factory=dict)


@router.patch("/text_templates")
async def patch_text_templates(body: TextTemplatesBody, auth: AuthDep) -> dict[str, Any]:
    admin, session = auth
    repo = AppSettingsRepository(session)
    if not body.templates:
        return {"ok": True}
    # Validate keys against the catalogue so a typo doesn't silently
    # store dead overrides.
    valid_keys = {t["key"] for t in catalogue_dict()}
    unknown = [k for k in body.templates if k not in valid_keys]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown keys: {unknown[:5]}")

    await repo.update_text_templates({k: (v or "") for k, v in body.templates.items()})
    clear_text_template_cache()
    try:
        await AuditLogRepository(session).log_action(
            actor_user_id=None,
            action="dashboard_text_templates_update",
            entity_type="app_settings",
            entity_id=admin.id,
            payload={
                "dashboard_admin": admin.username,
                "keys_changed": list(body.templates.keys()),
            },
        )
    except Exception as exc:
        logger.warning("audit log failed: %s", exc)
    await session.commit()
    return {"ok": True}
