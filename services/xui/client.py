from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from schemas.internal.xui import (
    XUIAPIResponse,
    XUIAddClientRequest,
    XUIClient,
    XUIClientTraffic,
    XUIInbound,
    XUILoginRequest,
    XUILoginResponse,
    XUIUpdateClientRequest,
)


class XUIClientError(Exception):
    pass


class XUIAuthenticationError(XUIClientError):
    pass


class XUIRequestError(XUIClientError):
    """``status_code`` is set when the panel answered with an HTTP error status
    (None for transport failures and success=false payloads), so callers can
    classify e.g. 404 "client gone" without substring-matching the message."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class XUIClientAbsentError(XUIRequestError):
    """A complete inventory confirms the client is absent from this panel."""


def _client_absence_confirmed(
    inbounds: list[XUIInbound], inbound_id: int, client_id: str, client: XUIClient,
) -> bool:
    # Missing mappings, partial inventories, moved identities, and reused emails
    # must remain errors. Only a positive, complete read can satisfy a disable.
    if not client_id or not client.email or not any(i.id == inbound_id for i in inbounds):
        return False
    identities = (client_id, client.id, client.uuid)
    for inbound in inbounds:
        if (inbound.protocol or "vless").lower() not in {"vless", "vmess"}:
            return False
        if not isinstance(inbound.settings, dict):
            return False
        clients = inbound.settings.get("clients")
        if not isinstance(clients, list):
            return False
        for existing in clients:
            if (
                not isinstance(existing, dict)
                or not isinstance(existing.get("id"), str)
                or not existing["id"]
                or not isinstance(existing.get("email"), str)
                or not existing["email"]
            ):
                return False
            if (
                existing["id"] in identities
                or (existing.get("uuid") is not None and existing["uuid"] in identities)
                or existing["email"] == client.email
            ):
                return False
    return True


@dataclass(slots=True, frozen=True)
class XUIClientConfig:
    base_url: str
    username: str
    password: SecretStr
    timeout_seconds: float = 20.0
    verify_ssl: bool = True  # Default True; set XUI_VERIFY_SSL=false explicitly for self-signed panels


class SanaeiXUIClient:
    def __init__(self, config: XUIClientConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = http_client is None
        if not config.verify_ssl:
            import logging
            logging.getLogger(__name__).warning(
                "X-UI client TLS verification DISABLED for %s — set XUI_VERIFY_SSL=true to enable",
                config.base_url,
            )
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            follow_redirects=True,
            verify=config.verify_ssl,
        )
        from services.panels.transport import enforce_https_redirects
        enforce_https_redirects(self._client, config.base_url, XUIRequestError)
        self._authenticated = False
        self._login_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._auth_generation = 0
        self._client.headers["X-Requested-With"] = "XMLHttpRequest"

    async def __aenter__(self) -> "SanaeiXUIClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def login(self) -> XUILoginResponse:
        async with self._login_lock:
            return await self._login_unlocked()

    async def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            async with self._recovery_lock:
                if not self._authenticated:
                    await self.login()

    async def _refresh_auth(self, generation: int) -> None:
        async with self._recovery_lock:
            if self._auth_generation == generation or not self._authenticated:
                self._authenticated = False
                await self.login()

    async def _login_unlocked(self) -> XUILoginResponse:
        self._authenticated = False
        self._client.cookies.clear()
        self._client.headers.pop("X-CSRF-Token", None)
        login_data = {
            "username": self._config.username,
            "password": self._config.password.get_secret_value(),
        }
        # Try JSON login first (newer X-UI versions)
        # Login is idempotent (re-login just issues a fresh session), so it
        # keeps the full timeout-retry behaviour.
        try:
            response = await self._send("POST", "login", json=login_data, idempotent=True)
        except XUIRequestError:
            # Fallback: try form-encoded login (older X-UI versions)
            response = await self._send(
                "POST", "login",
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                idempotent=True,
            )

        if response.status_code != 200:
            raise XUIAuthenticationError(f"X-UI login rejected (HTTP {response.status_code}).")
        try:
            data = self._decode_response(response)
        except XUIRequestError as exc:
            # Legacy HTML login is accepted only when this response sets a cookie.
            if not response.headers.get("set-cookie") or not self._client.cookies:
                raise XUIAuthenticationError("X-UI login returned no valid session.") from exc
            data = {"success": True}
        if not isinstance(data, dict) or data.get("success") is not True:
            raise XUIAuthenticationError("X-UI authentication failed; verify credentials and panel policy.")
        login_response = XUILoginResponse.model_validate(data)
        self._authenticated = True
        self._auth_generation += 1
        return login_response

    async def get_inbounds(self) -> list[XUIInbound]:
        response = await self._request("GET", "panel/api/inbounds/list")
        if (
            not isinstance(response, dict)
            or response.get("success") is not True
            or not isinstance(response.get("obj"), list)
        ):
            raise XUIRequestError("Panel did not return a complete successful inbound inventory.")
        api_response = XUIAPIResponse[list[dict[str, Any]]].model_validate(response)
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to fetch inbounds.")
        return [XUIInbound.model_validate(item) for item in api_response.obj or []]

    async def add_client_to_inbound(self, inbound_id: int, client: XUIClient) -> XUIAPIResponse[Any]:
        payload = XUIAddClientRequest.from_client(inbound_id, client)
        response = await self._request(
            "POST",
            "panel/api/inbounds/addClient",
            json=payload.model_dump(mode="json"),
        )
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to add inbound client.")
        return api_response

    async def patch_client(
        self, *, inbound_id: int, client_id: str, client: XUIClient,
        existing_settings: dict[str, Any] | None = None,
    ) -> XUIAPIResponse[Any]:
        """Preserve panel-owned and unknown fields on a full-map replacement."""
        if existing_settings is None:
            inbounds = await self.get_inbounds()
        else:
            # Usage sync already read this client's settings in its one batched
            # snapshot. Reuse it instead of downloading every inbound per user.
            inbounds = [XUIInbound(
                id=inbound_id, protocol="vless", settings={"clients": [existing_settings]},
            )]
        for inbound in inbounds:
            if inbound.id != inbound_id:
                continue
            if (inbound.protocol or "vless").lower() not in {"vless", "vmess"}:
                raise XUIRequestError("Unsupported client protocol.")
            settings = inbound.settings if isinstance(inbound.settings, dict) else {}
            for existing in settings.get("clients", []):
                if not isinstance(existing, dict) or str(existing.get("id")) != client_id:
                    continue
                changes = client.model_dump(
                    mode="json", by_alias=True, exclude_unset=True, exclude={"uuid"},
                )
                merged = XUIClient.model_validate({**existing, **changes})
                return await self.update_client(
                    inbound_id=inbound_id, client_id=client_id, client=merged,
                )
        if existing_settings is None and _client_absence_confirmed(
            inbounds, inbound_id, client_id, client,
        ):
            raise XUIClientAbsentError("Panel inventory confirms the client is absent.")
        raise XUIRequestError("Panel client was not found; refusing a blind overwrite.")

    async def update_client(
        self,
        *,
        inbound_id: int,
        client_id: str,
        client: XUIClient,
    ) -> XUIAPIResponse[Any]:
        payload = XUIUpdateClientRequest.from_client(inbound_id, client)
        # Full overwrite of one client with the same payload — safe to re-send.
        response = await self._request(
            "POST",
            f"panel/api/inbounds/updateClient/{client_id}",
            json=payload.model_dump(mode="json"),
            idempotent=True,
        )
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to update inbound client.")
        return api_response

    async def delete_client(self, *, inbound_id: int, client_id: str) -> XUIAPIResponse[Any]:
        response = await self._request(
            "POST",
            f"panel/api/inbounds/{inbound_id}/delClient/{client_id}",
        )
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to delete inbound client.")
        return api_response

    async def restart_xray_core(self) -> XUIAPIResponse[Any]:
        # Try Sanaei specific endpoint first
        try:
            response = await self._request("POST", "panel/api/server/restartXrayService")
        except XUIRequestError as e:
            # If the server disconnected, it means the restart was successful and the process was killed!
            if "RemoteProtocolError" in str(e) or "Server disconnected" in str(e):
                return XUIAPIResponse(success=True, msg="Restarted successfully (connection dropped)", obj=None)
            # Fallback to standard x-ui endpoint
            if "status 404" in str(e):
                try:
                    response = await self._request("POST", "server/restartXrayService")
                except XUIRequestError as e2:
                    if "RemoteProtocolError" in str(e2) or "Server disconnected" in str(e2):
                        return XUIAPIResponse(success=True, msg="Restarted successfully (connection dropped)", obj=None)
                    raise e2
            else:
                raise

        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to restart Xray core.")
        return api_response

    async def get_client_traffic(self, email: str) -> XUIClientTraffic:
        response = await self._request("GET", f"panel/api/inbounds/getClientTraffics/{quote(email, safe='')}")
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[dict[str, Any] | list[dict[str, Any]]].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch traffic.")
            payload = wrapper.obj
        else:
            payload = response

        # Handle null/empty response (new client with no traffic yet)
        if payload is None:
            return XUIClientTraffic(email=email, up=0, down=0)
        if isinstance(payload, list):
            if not payload:
                return XUIClientTraffic(email=email, up=0, down=0)
            payload = payload[0]
        if not isinstance(payload, dict):
            return XUIClientTraffic(email=email, up=0, down=0)
        return XUIClientTraffic.model_validate(payload)

    async def get_client_ips(self, email: str) -> list[str]:
        # Read-only despite being a POST — safe to re-send.
        response = await self._request("POST", f"panel/api/inbounds/clientIps/{quote(email, safe='')}", idempotent=True)
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[Any].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch client IPs.")
            payload = wrapper.obj
        else:
            payload = response
        return self._normalize_client_ips(payload)

    async def clear_client_ips(self, email: str) -> XUIAPIResponse[Any]:
        # Clearing twice yields the same state — safe to re-send.
        response = await self._request("POST", f"panel/api/inbounds/clearClientIps/{quote(email, safe='')}", idempotent=True)
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to clear client IPs.")
        return api_response

    async def reset_client_traffic(self, *, inbound_id: int, email: str) -> XUIAPIResponse[Any]:
        """Reset a client's cumulative traffic counter to 0 (3x-ui endpoint:
        POST /panel/api/inbounds/{id}/resetClientTraffic/{email}).

        Used by the plan renewal ("fresh start") flow so a renewed quota starts
        from zero. NOT marked idempotent: a blind retry could zero usage that
        accumulated after the first reset, and mutating calls must not be
        auto-retried (same rule as update_client)."""
        response = await self._request(
            "POST", f"panel/api/inbounds/{inbound_id}/resetClientTraffic/{quote(email, safe='')}",
            idempotent=False,
        )
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to reset client traffic.")
        return api_response

    async def get_panel_settings(self) -> dict[str, Any]:
        # Read-only despite being a POST — safe to re-send.
        response = await self._request("POST", "panel/setting/all", idempotent=True)
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[dict[str, Any]].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch panel settings.")
            return wrapper.obj or {}
        return {}

    async def get_db_backup(self) -> bytes:
        """Download X-UI panel database backup, trying known endpoints.

        3x-ui mounts ServerController under /panel/api/server, so getDb lives
        at panel/api/server/getDb. The other paths cover forks/older versions.

        Note on 404: the /panel/api group's checkAPIAuth middleware aborts with
        **404** (not 401) when the session is missing, deliberately hiding the
        endpoint from unauthenticated callers. Clients are pooled and long-lived,
        so by the time the 6-hourly backup runs the cookie has usually expired —
        the panel then answers 404 and, unless we re-login on 404 too, every
        endpoint "fails" and the DB is silently dropped from the bundle.
        """
        await self._ensure_authenticated()

        endpoints = [
            "panel/api/server/getDb",   # 3x-ui (current)
            "server/getDb",             # some forks
            "panel/setting/getDb",      # legacy
            "xui/API/inbounds/getDb",   # very old
        ]
        attempts: list[str] = []

        for endpoint in endpoints:
            generation = self._auth_generation
            response = await self._client.request("GET", endpoint)
            # 404 is included on purpose — see the docstring.
            if response.status_code in {401, 403, 404} or self._is_login_response(response):
                await self._refresh_auth(generation)
                response = await self._client.request("GET", endpoint)

            if response.status_code == 200 and response.content.startswith(b"SQLite format 3\x00") and len(response.content) >= 100:
                return response.content
            attempts.append(
                f"{endpoint} → HTTP {response.status_code}, len={len(response.content)}"
            )

        raise XUIRequestError(
            "Failed to download X-UI DB from any endpoint. Attempts: "
            + "; ".join(attempts)
        )

    async def _request(
        self, method: str, path: str, *, idempotent: bool | None = None, **kwargs: Any
    ) -> dict[str, Any] | list[Any] | None:
        await self._ensure_authenticated()
        generation = self._auth_generation
        response = await self._send_with_csrf(method, path, idempotent=idempotent, **kwargs)
        auth_failed = response.status_code in {401, 403} or self._is_login_response(response)
        if response.status_code == 404:
            # A write can be retried only after a read confirms API auth was
            # rejected; an actual missing endpoint/client must stay a 404.
            if method.upper() in {"GET", "HEAD"} or idempotent:
                auth_failed = True
            else:
                probe = await self._send("GET", "panel/api/inbounds/list", idempotent=True)
                auth_failed = probe.status_code in {401, 403, 404} or self._is_login_response(probe)
        if auth_failed:
            await self._refresh_auth(generation)
            response = await self._send_with_csrf(method, path, idempotent=idempotent, **kwargs)
            if response.status_code in {401, 403} or self._is_login_response(response):
                self._authenticated = False
                raise XUIAuthenticationError("X-UI session rejected after renewal; check credentials and panel policy.")
        if response.status_code == 404:
            # Establish valid authentication before allowing 'already gone'.
            probe = await self._send("GET", "panel/api/inbounds/list", idempotent=True)
            if probe.status_code != 200 or self._is_login_response(probe):
                self._authenticated = False
                raise XUIAuthenticationError("Cannot distinguish X-UI auth failure from a missing endpoint.")
            probe_data = self._decode_response(probe)
            if not isinstance(probe_data, dict) or probe_data.get("success") is not True:
                raise XUIRequestError("X-UI authentication probe did not confirm a valid API response.")
            raise XUIRequestError(f"X-UI endpoint '{path}' not found after verified authentication.", status_code=404)
        return self._decode_response(response)

    @staticmethod
    def _is_login_response(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            body = response.text.lower()
            return any(marker in body for marker in ("login", "sign in", 'type="password"', "type='password'"))
        if "application/json" in content_type:
            try:
                data = response.json()
            except ValueError:
                return False
            if isinstance(data, dict) and data.get("success") is False:
                msg = str(data.get("msg", "")).lower()
                return any(marker in msg for marker in ("not logged", "not login", "unauthorized", "unauthenticated", "session expired", "login required", "please login"))
        return False

    async def _send_with_csrf(self, method, path, *, idempotent=None, **kwargs):
        response = await self._send(method, path, idempotent=idempotent, **kwargs)
        if response.status_code == 403 and "csrf" in response.text.lower():
            await self._refresh_csrf()
            response = await self._send(method, path, idempotent=idempotent, **kwargs)
            if response.status_code == 403 and "csrf" in response.text.lower():
                raise XUIAuthenticationError("X-UI rejected the CSRF token; check panel compatibility.")
        return response

    async def _refresh_csrf(self) -> None:
        response = await self._send("GET", "csrf-token", idempotent=True)
        if response.status_code != 200:
            raise XUIAuthenticationError("X-UI CSRF token endpoint is unavailable.")
        data = self._decode_response(response)
        if not isinstance(data, dict):
            raise XUIAuthenticationError("X-UI returned an invalid CSRF token.")
        obj = data.get("obj", data)
        token = obj if isinstance(obj, str) else (obj.get("token") or obj.get("csrfToken") or obj.get("csrf_token")) if isinstance(obj, dict) else None
        if not isinstance(token, str) or not token:
            raise XUIAuthenticationError("X-UI returned no CSRF token.")
        self._client.headers["X-CSRF-Token"] = token

    async def _send(
        self, method: str, path: str, *, idempotent: bool | None = None, **kwargs: Any
    ) -> httpx.Response:
        started = time.monotonic()
        # Static operation only: do not log client UUIDs, emails or sub tokens.
        operation = "/".join(path.strip("/").split("/")[:4])
        try:
            return await self._send_with_retries(method, path, idempotent=idempotent, **kwargs)
        except Exception as exc:
            cause = exc.__cause__ or exc
            logging.getLogger(__name__).warning(
                "Panel request failed operation=%s method=%s error=%s status=%s "
                "elapsed_ms=%d auth_generation=%d tls_verify=%s",
                operation, method, type(cause).__name__, getattr(exc, "status_code", None),
                int((time.monotonic() - started) * 1000), self._auth_generation,
                self._config.verify_ssl,
            )
            raise

    async def _send_with_retries(
        self, method: str, path: str, *, idempotent: bool | None = None, **kwargs: Any
    ) -> httpx.Response:
        import asyncio
        max_retries = 3
        # Mutating POSTs (addClient/delClient/...) must NOT be blindly re-sent
        # after a mid-flight timeout/disconnect: the panel may have already
        # committed the first attempt, so the retry yields 'Duplicate email'
        # (orphan panel client) or 'client not found' (masking a successful
        # delete). Non-idempotent requests are therefore only retried on
        # connection-phase failures, where the request provably never reached
        # the panel. Read-only/overwrite POSTs opt in via idempotent=True.
        if idempotent is None:
            idempotent = method.upper() in {"GET", "HEAD"}
        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)
                # Auth errors must be returned (not raised) so _request can
                # re-login and retry. raise_for_status() would throw them out
                # of _send as XUIRequestError before _request ever sees the
                # 401/403 — which made the re-login path dead code and broke
                # every panel op once the session cookie expired.
                if response.status_code in {401, 403} or response.status_code == 404:
                    return response
                response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                never_sent = isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
                if (idempotent or never_sent) and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise XUIRequestError(f"Timed out while calling X-UI endpoint '{path}'.") from exc
            except httpx.HTTPStatusError as exc:
                if idempotent and exc.response.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise XUIRequestError(
                    f"X-UI request to '{path}' failed with status {exc.response.status_code}: "
                    f"{self._safe_response_text(exc.response)}",
                    status_code=exc.response.status_code,
                ) from exc
            except httpx.RequestError as exc:
                never_sent = isinstance(exc, httpx.ConnectError)
                if (idempotent or never_sent) and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                cause = f" (caused by {type(exc.__cause__).__name__}: {exc.__cause__})" if exc.__cause__ else ""
                raise XUIRequestError(
                    f"{type(exc).__name__} while calling X-UI endpoint '{path}'{cause}"
                ) from exc
        # Should not reach here
        raise XUIRequestError(f"All {max_retries} retries exhausted for X-UI endpoint '{path}'.")

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
        if not response.content or not response.text.strip():
            return None
        if "application/json" not in response.headers.get("content-type", ""):
            raise XUIRequestError(f"Unexpected response content type: {response.headers.get('content-type')}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise XUIRequestError("Invalid JSON response from X-UI.") from exc
        if not isinstance(payload, (dict, list)):
            raise XUIRequestError("Unexpected X-UI JSON payload type.")
        return payload

    @staticmethod
    def _safe_response_text(response: httpx.Response) -> str:
        return response.text[:500].strip() or "<empty response>"

    @staticmethod
    def _normalize_client_ips(payload: Any) -> list[str]:
        if payload is None:
            return []
        if isinstance(payload, str):
            stripped = payload.strip()
            if not stripped:
                return []
            parts = stripped.replace(",", "\n").splitlines()
            return list(dict.fromkeys(part.strip() for part in parts if part.strip()))
        if isinstance(payload, dict):
            for key in ("ips", "clientIps", "client_ips"):
                value = payload.get(key)
                if isinstance(value, list):
                    return SanaeiXUIClient._normalize_client_ips(value)
            value = payload.get("ip") or payload.get("address")
            return SanaeiXUIClient._normalize_client_ips(value)
        if isinstance(payload, list):
            ips: list[str] = []
            for item in payload:
                if isinstance(item, dict):
                    value = item.get("ip") or item.get("address")
                    if value:
                        ips.extend(SanaeiXUIClient._normalize_client_ips(value))
                elif isinstance(item, str):
                    ips.extend(SanaeiXUIClient._normalize_client_ips(item))
            return list(dict.fromkeys(ips))
        return []
