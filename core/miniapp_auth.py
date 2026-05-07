from __future__ import annotations

import base64
import hashlib
import hmac
import time

from core.config import settings


DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def create_miniapp_session_token(telegram_id: int, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    expires_at = int(time.time()) + ttl_seconds
    payload = f"{telegram_id}:{expires_at}"
    signature = _sign(payload)
    raw_token = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw_token).decode("ascii").rstrip("=")


def verify_miniapp_session_token(token: str) -> int | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        telegram_id_raw, expires_at_raw, signature = decoded.split(":", 2)
        payload = f"{telegram_id_raw}:{expires_at_raw}"
        if not hmac.compare_digest(_sign(payload), signature):
            return None
        if int(expires_at_raw) < int(time.time()):
            return None
        return int(telegram_id_raw)
    except (ValueError, UnicodeDecodeError):
        return None


def _sign(payload: str) -> str:
    secret = settings.app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
