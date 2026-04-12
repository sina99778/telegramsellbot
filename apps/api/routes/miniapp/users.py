from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.dependencies.db import get_db_session
from core.config import settings
from models.user import User
from schemas.api.miniapp import MiniAppDashboardResponse, SubscriptionView, WalletView


router = APIRouter()


@router.get("/me", response_model=MiniAppDashboardResponse)
async def get_me(
    init_data: str = Header(alias="X-Telegram-Init-Data"),
    session: AsyncSession = Depends(get_db_session),
) -> MiniAppDashboardResponse:
    telegram_user_id = validate_telegram_init_data(init_data)

    query = (
        select(User)
        .options(
            selectinload(User.wallet),
            selectinload(User.subscriptions),
        )
        .where(User.telegram_id == telegram_user_id)
    )
    result = await session.execute(query)
    user = result.scalar_one_or_none()

    if user is None or user.wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    return MiniAppDashboardResponse(
        user_id=user.id,
        telegram_id=user.telegram_id,
        wallet=WalletView.model_validate(user.wallet),
        subscriptions=[SubscriptionView.model_validate(item) for item in user.subscriptions],
    )


def validate_telegram_init_data(init_data: str) -> int:
    parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = parsed_data.pop("hash", None)

    if not provided_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram hash.",
        )

    auth_date_raw = parsed_data.get("auth_date")
    if auth_date_raw is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram auth_date.",
        )

    try:
        auth_date = datetime.fromtimestamp(int(auth_date_raw), tz=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram auth_date.",
        ) from exc

    if (datetime.now(timezone.utc) - auth_date).total_seconds() > 24 * 60 * 60:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram init data is expired.",
        )

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=settings.bot_token.get_secret_value().encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed_data.items())
    )
    expected_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, provided_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram init data signature.",
        )

    user_payload_raw = parsed_data.get("user")
    if user_payload_raw is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram user payload.",
        )

    try:
        user_payload = json.loads(user_payload_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram user payload.",
        ) from exc

    telegram_id = _extract_telegram_user_id(user_payload)
    if telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram user id is missing.",
        )

    return telegram_id


def _extract_telegram_user_id(user_payload: Mapping[str, object]) -> int | None:
    raw_id = user_payload.get("id")
    if raw_id is None:
        return None

    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None
