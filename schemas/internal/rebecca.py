"""
Rebecca panel API schemas (internal).

Rebecca is ANOTHER Marzban-derived, user-centric panel (forked from Marzban) —
the same family as PasarGuard. Its user CRUD / token / response shapes are
identical to PasarGuard's, so we REUSE those (PGToken/PGUserModify/PGUserResponse
+ make_absolute_sub_url). The only real differences:
  * the inbound bundle is a "service" (GET /api/v2/services) not a "group".
  * create assigns a single `service_id` (not a list of group_ids).

Verified against rebeccapanel/Rebecca (Marzban-compatible API):
- POST /api/admin/token (OAuth2 form) -> {access_token}
- GET  /api/v2/services -> {services:[{id,name,host_count}],total}
- POST /api/user (service mode: service_id) -> 201 UserResponse
- GET/PUT/DELETE /api/user/{username}; POST /api/user/{username}/reset|revoke_sub
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Marzban-family shapes shared verbatim with PasarGuard.
from schemas.internal.pasarguard import (  # noqa: F401  (re-exported for the client)
    PGToken as RebeccaToken,
    PGUserModify as RebeccaUserModify,
    PGUserResponse as RebeccaUser,
    make_absolute_sub_url,
)


# ─── Services (inbound bundles) ───────────────────────────────────────────────


class RebeccaService(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    name: str
    host_count: int = 0
    is_disabled: bool = False  # Rebecca ServiceBase has no disable flag → default active


class RebeccaServicesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    services: list[RebeccaService] = Field(default_factory=list)
    total: int = 0


# ─── User create (service mode) ───────────────────────────────────────────────


class RebeccaUserCreate(BaseModel):
    """Body for POST /api/user in SERVICE mode (assign to one service bundle).

    Same Marzban fields as PasarGuard's create, except the bundle is a single
    `service_id` rather than `group_ids`. `to_payload()` drops None so an
    on_hold user omits `expire` (uses on_hold_expire_duration instead)."""

    model_config = ConfigDict(extra="allow")

    username: str
    status: str = "active"  # "active" | "on_hold"
    expire: int | None = None  # unix seconds; None/0 == unlimited
    data_limit: int | None = None  # bytes; None/0 == unlimited
    data_limit_reset_strategy: str = "no_reset"
    service_id: int | None = None
    on_hold_expire_duration: int | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
