from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db_session
from models.payment import Payment
from models.user import User


router = APIRouter()


@router.get("/overview")
async def admin_overview(
    owner_telegram_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int | str]:
    if settings.owner_telegram_id is None or owner_telegram_id != settings.owner_telegram_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    users_count = await session.scalar(select(func.count()).select_from(User))
    payments_count = await session.scalar(select(func.count()).select_from(Payment))

    return {
        "status": "ok",
        "users_count": int(users_count or 0),
        "payments_count": int(payments_count or 0),
    }
