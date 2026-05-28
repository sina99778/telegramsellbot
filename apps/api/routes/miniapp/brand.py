"""
Mini-app brand endpoint.

`GET /api/miniapp/brand` returns the operator-set brand identity so
the mini-app can theme its header / accent color without a deploy.
No auth — these are public branding bits.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory
from repositories.settings import AppSettingsRepository


router = APIRouter()


async def _open_session() -> AsyncSession:
    async with AsyncSessionFactory() as s:
        yield s


@router.get("/brand")
async def get_public_brand(session: AsyncSession = Depends(_open_session)) -> dict[str, Any]:
    b = await AppSettingsRepository(session).get_brand_settings()
    return {
        "name": b.name,
        "logo_url": b.logo_url,
        "accent_color": b.accent_color,
        "support_handle": b.support_handle,
    }
