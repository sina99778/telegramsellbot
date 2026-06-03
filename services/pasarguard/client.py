"""
Async HTTP client for the PasarGuard panel.

Mirrors services/xui/client.py (dataclass config, owned httpx.AsyncClient,
retry/backoff `_send`, `__aenter__/__aexit__/aclose`) but speaks PasarGuard's
token-based REST API instead of X-UI's cookie/session API:

- login -> POST /api/admin/token (OAuth2 password *form*), cache the bearer
  token on the client's Authorization header; re-login once on 401/403.
- user CRUD + usage via /api/user/{username}; groups via /api/groups.

Errors mirror the X-UI naming (PasarGuard{,Auth,Request}Error). RequestError
carries `.status_code` so callers can branch (e.g. treat 404 as "already gone").
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from schemas.internal.pasarguard import (
    PGGroup,
    PGGroupsResponse,
    PGToken,
    PGUserCreate,
    PGUserModify,
    PGUserResponse,
)


logger = logging.getLogger(__name__)


class PasarGuardError(Exception):
    pass


class PasarGuardAuthError(PasarGuardError):
    pass


class PasarGuardRequestError(PasarGuardError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True, frozen=True)
class PasarGuardClientConfig:
    base_url: str
    username: str
    password: SecretStr
    timeout_seconds: float = 20.0
    verify_ssl: bool = True


def _origin(base_url: str) -> str:
    """scheme://host[:port] from a URL, ignoring any path. PasarGuard serves its
    API at the origin root (/api/...), so we never want an accidental path."""
    raw = base_url if "://" in base_url else f"http://{base_url}"
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    return f"{scheme}://{parts.netloc}"


class PasarGuardClient:
    def __init__(
        self,
        config: PasarGuardClientConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._origin = _origin(config.base_url)
        if not config.verify_ssl:
            logger.warning(
                "PasarGuard client TLS verification DISABLED for %s — set "
                "PASARGUARD_VERIFY_SSL=true to enable",
                self._origin,
            )
        self._client = http_client or httpx.AsyncClient(
            base_url=self._origin + "/",
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
            verify=config.verify_ssl,
        )
        self._authenticated = False
        self._token: str | None = None

    async def __aenter__(self) -> "PasarGuardClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── auth ─────────────────────────────────────────────────────────────────

    async def login(self) -> PGToken:
        """POST /api/admin/token (OAuth2 password grant, form-encoded)."""
        response = await self._send(
            "POST",
            "api/admin/token",
            data={
                "username": self._config.username,
                "password": self._config.password.get_secret_value(),
                "grant_type": "password",
            },
        )
        if response.status_code != 200:
            raise PasarGuardAuthError(
                "PasarGuard login failed "
                f"(HTTP {response.status_code}: {self._safe_text(response)}). "
                "Verify the URL, username, and password."
            )
        try:
            token = PGToken.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001
            raise PasarGuardAuthError("PasarGuard returned an invalid token payload.") from exc
        self._token = token.access_token
        self._client.headers["Authorization"] = f"Bearer {token.access_token}"
        self._authenticated = True
        return token

    async def get_current_admin(self) -> dict[str, Any]:
        """GET /api/admin — used as a token/health probe."""
        response = await self._request("GET", "api/admin")
        return response.json() if response.content else {}

    # ── groups ───────────────────────────────────────────────────────────────

    async def get_groups(self) -> list[PGGroup]:
        """GET /api/groups → all groups (inbound bundles)."""
        response = await self._request("GET", "api/groups")
        return PGGroupsResponse.model_validate(response.json()).groups

    # ── users (== configs) ───────────────────────────────────────────────────

    async def create_user(self, payload: PGUserCreate) -> PGUserResponse:
        response = await self._request(
            "POST", "api/user", json=payload.to_payload(), expected=(200, 201)
        )
        return PGUserResponse.model_validate(response.json())

    async def get_user(self, username: str) -> PGUserResponse | None:
        """GET /api/user/{username}. Returns None when the user no longer exists
        on the panel (404) — callers treat that as 'gone'."""
        response = await self._request(
            "GET", f"api/user/{username}", expected=(200, 404)
        )
        if response.status_code == 404:
            return None
        return PGUserResponse.model_validate(response.json())

    async def modify_user(self, username: str, payload: PGUserModify) -> PGUserResponse:
        response = await self._request(
            "PUT", f"api/user/{username}", json=payload.to_payload()
        )
        return PGUserResponse.model_validate(response.json())

    async def delete_user(self, username: str) -> None:
        """DELETE /api/user/{username}. 404 is treated as already-deleted."""
        await self._request(
            "DELETE", f"api/user/{username}", expected=(200, 204, 404)
        )

    async def reset_user_usage(self, username: str) -> PGUserResponse:
        response = await self._request("POST", f"api/user/{username}/reset")
        return PGUserResponse.model_validate(response.json())

    async def revoke_sub(self, username: str) -> PGUserResponse:
        """Rotate the subscription link (and proxy ids) for a user."""
        response = await self._request("POST", f"api/user/{username}/revoke_sub")
        return PGUserResponse.model_validate(response.json())

    # ── plumbing ─────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200, 201, 204),
        **kwargs: Any,
    ) -> httpx.Response:
        if not self._authenticated:
            await self.login()

        response = await self._send(method, path, **kwargs)
        if response.status_code in {401, 403}:
            # Token likely expired — re-auth once and retry.
            self._authenticated = False
            await self.login()
            response = await self._send(method, path, **kwargs)

        if response.status_code not in expected:
            raise PasarGuardRequestError(
                f"PasarGuard request to '{path}' failed with status "
                f"{response.status_code}: {self._safe_text(response)}",
                status_code=response.status_code,
            )
        return response

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        max_retries = 3
        last_response: httpx.Response | None = None
        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise PasarGuardRequestError(
                    f"Timed out while calling PasarGuard endpoint '{path}'."
                ) from exc
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                cause = (
                    f" (caused by {type(exc.__cause__).__name__}: {exc.__cause__})"
                    if exc.__cause__
                    else ""
                )
                raise PasarGuardRequestError(
                    f"{type(exc).__name__} while calling PasarGuard endpoint '{path}'{cause}"
                ) from exc

            # Retry transient 5xx; return everything else (incl. 4xx) for the
            # caller (_request) to interpret.
            if response.status_code >= 500 and attempt < max_retries - 1:
                last_response = response
                await asyncio.sleep(2 ** attempt)
                continue
            return response

        if last_response is not None:
            return last_response
        raise PasarGuardRequestError(f"All {max_retries} retries exhausted for '{path}'.")

    @staticmethod
    def _safe_text(response: httpx.Response) -> str:
        try:
            return response.text[:500].strip() or "<empty response>"
        except Exception:  # noqa: BLE001
            return "<unreadable response>"
