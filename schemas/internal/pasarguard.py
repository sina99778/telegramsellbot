"""
PasarGuard panel API schemas (internal).

PasarGuard is a Marzban-derived, USER-CENTRIC panel: one "user" == one
config/subscription. We model only the slice of its API the bot needs:
admin token, groups (inbound bundles), and user CRUD/usage. Mirrors the style
of schemas/internal/xui.py (populate_by_name + extra="allow" so unknown panel
fields never break parsing).

Verified against PasarGuard/panel `main` (v3):
- POST /api/admin/token (OAuth2 form) -> {access_token, token_type}
- GET  /api/groups -> {groups:[{id,name,inbound_tags,is_disabled,total_users}],total}
- POST /api/user (UserCreate) -> 201 UserResponse
- GET/PUT/DELETE /api/user/{username}; POST /api/user/{username}/reset|revoke_sub
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


def make_absolute_sub_url(base_url: str, subscription_url: str | None) -> str:
    """Resolve a PasarGuard `subscription_url` to an absolute URL.

    PasarGuard may return a RELATIVE path (e.g. "/sub/<token>/"); prefix it with
    the panel's scheme://host[:port]. Absolute URLs (already http/https) and
    empty values pass through unchanged. Shared by the response model and the
    runtime helper so the rule lives in exactly one place.
    """
    url = (subscription_url or "").strip()
    if not url or url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
    origin = f"{parts.scheme or 'http'}://{parts.netloc}"
    if not url.startswith("/"):
        url = "/" + url
    return origin + url


# ─── Auth ─────────────────────────────────────────────────────────────────────


class PGToken(BaseModel):
    model_config = ConfigDict(extra="allow")

    access_token: str
    token_type: str = "bearer"


# ─── Groups (inbound bundles) ─────────────────────────────────────────────────


class PGGroup(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str
    inbound_tags: list[str] = Field(default_factory=list)
    is_disabled: bool = False
    total_users: int = 0


class PGGroupsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    groups: list[PGGroup] = Field(default_factory=list)
    total: int = 0


# ─── User create / modify ─────────────────────────────────────────────────────


class PGUserCreate(BaseModel):
    """Body for POST /api/user.

    `proxy_settings` is intentionally NOT a field: omitting it makes PasarGuard
    auto-generate settings for ALL protocols. Use `to_payload()` which drops
    None values so we never send an explicit null where "omit" is meant
    (e.g. on_hold users must omit `expire`).
    """

    model_config = ConfigDict(extra="allow")

    username: str
    # "active" | "on_hold" (create only allows these two)
    status: str = "active"
    # Unix seconds (UTC). None/omitted or 0 == unlimited. For on_hold, omit this
    # and set on_hold_expire_duration instead.
    expire: int | None = None
    # Bytes. None/omitted or 0 == unlimited.
    data_limit: int | None = None
    data_limit_reset_strategy: str = "no_reset"
    group_ids: list[int] = Field(default_factory=list)
    # Seconds the on_hold timer runs once the user first connects.
    on_hold_expire_duration: int | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize for the API, dropping None so omitted == panel default."""
        return self.model_dump(exclude_none=True)


class PGUserModify(BaseModel):
    """Body for PUT /api/user/{username}. All optional — omitted == no change."""

    model_config = ConfigDict(extra="allow")

    status: str | None = None
    expire: int | None = None
    data_limit: int | None = None
    group_ids: list[int] | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


# ─── User response ────────────────────────────────────────────────────────────


class PGUserResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int | None = None
    username: str
    status: str = "active"
    used_traffic: int = 0
    lifetime_used_traffic: int = 0
    # The panel may serialize expire as a unix int OR an ISO datetime string.
    expire: int | str | None = None
    data_limit: int | None = None
    subscription_url: str = ""
    online_at: str | None = None

    @property
    def expire_ts(self) -> int | None:
        """Normalize `expire` (int seconds | ISO string | null/0) to a unix int,
        or None for 'unlimited'/unset."""
        v = self.expire
        if v in (None, 0, "", "0"):
            return None
        if isinstance(v, int):
            return v
        try:
            iso = str(v).replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_dead(self) -> bool:
        """True when the panel considers the config finished (expired/limited)."""
        return self.status in {"expired", "limited"}

    def absolute_subscription_url(self, base_url: str) -> str:
        """Full subscription URL (prefix the panel origin when relative)."""
        return make_absolute_sub_url(base_url, self.subscription_url)
