from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.database import utcnow
from models.user import User, UserProfile


PHONE_META_KEY = "phone_verification"
IRAN_PHONE_RE = re.compile(r"^(?:\+98|0098|98|0)?9\d{9}$")

# Verified phones are trusted for this long before we ask the user to
# verify again. Without an expiry, a stolen-then-recovered Telegram
# account stays "verified" forever.
PHONE_VERIFICATION_TTL = timedelta(days=180)


def normalize_phone_number(phone: str) -> str:
    return re.sub(r"[\s\-()]", "", phone.strip())


def is_valid_phone_number(phone: str, mode: str) -> bool:
    normalized = normalize_phone_number(phone)
    if mode == "iran":
        return bool(IRAN_PHONE_RE.fullmatch(normalized))
    digit_count = len(re.sub(r"\D", "", normalized))
    return 6 <= digit_count <= 16 and (normalized.startswith(("+", "00")) or normalized.isdigit())


def _load_profile_payload(profile: UserProfile) -> dict[str, object]:
    if not profile.notes:
        return {}
    try:
        payload = json.loads(profile.notes)
    except (TypeError, json.JSONDecodeError):
        return {"legacy_notes": profile.notes}
    return payload if isinstance(payload, dict) else {}


def get_verified_phone(user: User) -> str | None:
    """Return the user's verified phone or None.

    A phone is considered verified only if the verification is still within
    PHONE_VERIFICATION_TTL — older verifications are treated as expired and
    callers should re-prompt the user.
    """
    profile = user.profile
    if profile is None:
        return None
    payload = _load_profile_payload(profile)
    phone_meta = payload.get(PHONE_META_KEY)
    if not isinstance(phone_meta, dict):
        return None
    phone = phone_meta.get("phone")
    if not phone:
        return None
    verified_at_raw = phone_meta.get("verified_at")
    if isinstance(verified_at_raw, str):
        try:
            verified_at = datetime.fromisoformat(verified_at_raw)
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - verified_at > PHONE_VERIFICATION_TTL:
                return None
        except ValueError:
            # Bad timestamp on disk — treat as unverified rather than
            # crashing the caller.
            return None
    return str(phone)


async def set_verified_phone(session: AsyncSession, user: User, phone: str) -> None:
    profile = user.profile
    if profile is None:
        profile = UserProfile(user_id=user.id)
        session.add(profile)
        await session.flush()
        user.profile = profile

    payload = _load_profile_payload(profile)
    now = utcnow().isoformat()
    payload[PHONE_META_KEY] = {
        "phone": normalize_phone_number(phone),
        "verified_at": now,
    }
    profile.notes = json.dumps(payload, ensure_ascii=False)
    session.add(profile)
    await session.flush()
