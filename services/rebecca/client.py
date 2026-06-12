"""
Async HTTP client for the Rebecca panel (Marzban fork, user-centric).

Rebecca's user API is Marzban-compatible — identical to PasarGuard's for token
auth and user CRUD — so this client mirrors services/pasarguard/client.py. The
only differences: the inbound bundle is a "service" (GET /api/v2/services) and a
config is assigned a single `service_id`. User responses reuse the shared
Marzban schemas (RebeccaUser == PGUserResponse).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from schemas.internal.rebecca import (
    RebeccaServicesResponse,
    RebeccaToken,
    RebeccaUser,
    RebeccaUserCreate,
    RebeccaUserModify,
)


logger = logging.getLogger(__name__)


class RebeccaError(Exception):
    pass


class RebeccaAuthError(RebeccaError):
    pass


class RebeccaRequestError(RebeccaError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True, frozen=True)
class RebeccaClientConfig:
    base_url: str
    username: str
    password: SecretStr
    timeout_seconds: float = 20.0
    verify_ssl: bool = True


def _origin(base_url: str) -> str:
    raw = base_url if "://" in base_url else f"http://{base_url}"
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    return f"{scheme}://{parts.netloc}"


class RebeccaClient:
    def __init__(
        self,
        config: RebeccaClientConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._origin = _origin(config.base_url)
        if not config.verify_ssl:
            logger.warning(
                "Rebecca client TLS verification DISABLED for %s — set "
                "REBECCA_VERIFY_SSL=true to enable",
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

    async def __aenter__(self) -> "RebeccaClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── auth ─────────────────────────────────────────────────────────────────

    async def login(self) -> RebeccaToken:
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
            raise RebeccaAuthError(
                "Rebecca login failed "
                f"(HTTP {response.status_code}: {self._safe_text(response)}). "
                "Verify the URL, username, and password."
            )
        try:
            token = RebeccaToken.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001
            raise RebeccaAuthError("Rebecca returned an invalid token payload.") from exc
        self._token = token.access_token
        self._client.headers["Authorization"] = f"Bearer {token.access_token}"
        self._authenticated = True
        return token

    async def get_current_admin(self) -> dict[str, Any]:
        """GET /api/admin — used as a token/health probe (mirrors PasarGuard)."""
        response = await self._request("GET", "api/admin")
        return response.json() if response.content else {}

    # ── services (inbound bundles) ─────────────────────────────────────────────

    async def get_services(self):
        """GET /api/v2/services → all services (limit high enough for any operator)."""
        response = await self._request("GET", "api/v2/services", params={"offset": 0, "limit": 1000})
        return RebeccaServicesResponse.model_validate(response.json()).services

    # ── users (== configs) ───────────────────────────────────────────────────

    async def create_user(self, payload: RebeccaUserCreate) -> RebeccaUser:
        response = await self._request(
            "POST", "api/user", json=payload.to_payload(), expected=(200, 201)
        )
        return RebeccaUser.model_validate(response.json())

    async def get_user(self, username: str) -> RebeccaUser | None:
        response = await self._request("GET", f"api/user/{username}", expected=(200, 404))
        if response.status_code == 404:
            return None
        return RebeccaUser.model_validate(response.json())

    async def modify_user(self, username: str, payload: RebeccaUserModify) -> RebeccaUser:
        response = await self._request("PUT", f"api/user/{username}", json=payload.to_payload())
        return RebeccaUser.model_validate(response.json())

    async def delete_user(self, username: str) -> None:
        await self._request("DELETE", f"api/user/{username}", expected=(200, 204, 404))

    async def reset_user_usage(self, username: str) -> RebeccaUser:
        response = await self._request("POST", f"api/user/{username}/reset")
        return RebeccaUser.model_validate(response.json())

    async def revoke_sub(self, username: str) -> RebeccaUser:
        response = await self._request("POST", f"api/user/{username}/revoke_sub")
        return RebeccaUser.model_validate(response.json())

    # ── uniform Marzban-family interface (shared with PasarGuard) ─────────────

    async def list_bundles(self):
        from services.panels.base import RemoteGroup

        return [
            RemoteGroup(remote_id=s.id, name=s.name, is_disabled=s.is_disabled, tags=[])
            for s in await self.get_services()
        ]

    async def create_user_in_bundle(
        self,
        *,
        username: str,
        status: str,
        expire: int | None,
        data_limit: int | None,
        bundle_id: int,
        on_hold_expire_duration: int | None = None,
        note: str | None = None,
    ) -> RebeccaUser:
        """Create a config assigned to ONE bundle (Rebecca: service_id=bundle)."""
        return await self.create_user(
            RebeccaUserCreate(
                username=username,
                status=status,
                expire=expire,
                data_limit=data_limit,
                service_id=int(bundle_id),
                on_hold_expire_duration=on_hold_expire_duration,
                note=note,
            )
        )

    # ── plumbing (identical to PasarGuard) ────────────────────────────────────

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
            self._authenticated = False
            await self.login()
            response = await self._send(method, path, **kwargs)

        if response.status_code not in expected:
            raise RebeccaRequestError(
                f"Rebecca request to '{path}' failed with status "
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
                raise RebeccaRequestError(f"Timed out while calling Rebecca endpoint '{path}'.") from exc
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                cause = (
                    f" (caused by {type(exc.__cause__).__name__}: {exc.__cause__})"
                    if exc.__cause__
                    else ""
                )
                raise RebeccaRequestError(
                    f"{type(exc).__name__} while calling Rebecca endpoint '{path}'{cause}"
                ) from exc

            if response.status_code >= 500 and attempt < max_retries - 1:
                last_response = response
                await asyncio.sleep(2 ** attempt)
                continue
            return response

        if last_response is not None:
            return last_response
        raise RebeccaRequestError(f"All {max_retries} retries exhausted for '{path}'.")

    @staticmethod
    def _safe_text(response: httpx.Response) -> str:
        try:
            return response.text[:500].strip() or "<empty response>"
        except Exception:  # noqa: BLE001
            return "<unreadable response>"
