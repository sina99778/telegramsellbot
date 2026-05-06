from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    pass


@dataclass(slots=True, frozen=True)
class XUIClientConfig:
    base_url: str
    username: str
    password: SecretStr
    timeout_seconds: float = 20.0
    verify_ssl: bool = False  # Default False for self-signed X-UI certs


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
        self._authenticated = False

    async def __aenter__(self) -> "SanaeiXUIClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def login(self) -> XUILoginResponse:
        login_data = {
            "username": self._config.username,
            "password": self._config.password.get_secret_value(),
        }
        # Try JSON login first (newer X-UI versions)
        try:
            response = await self._send("POST", "login", json=login_data)
        except XUIRequestError:
            # Fallback: try form-encoded login (older X-UI versions)
            response = await self._send(
                "POST", "login",
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        # Some X-UI versions return HTML on login — check for cookies first
        if self._client.cookies:
            self._authenticated = True
            return XUILoginResponse(success=True, msg="OK")

        # Try to parse JSON response
        try:
            data = self._decode_response(response)
            login_response = XUILoginResponse.model_validate(data or {"success": True})
            if login_response.success is False:
                raise XUIAuthenticationError(login_response.msg or "X-UI authentication failed.")
        except XUIRequestError:
            # Response wasn't JSON but we also have no cookies — auth failed
            raise XUIAuthenticationError(
                "X-UI login failed. The panel did not return a session cookie. "
                "Please verify the URL, username, and password."
            )

        self._authenticated = True
        return login_response

    async def get_inbounds(self) -> list[XUIInbound]:
        response = await self._request("GET", "panel/api/inbounds/list")
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

    async def update_client(
        self,
        *,
        inbound_id: int,
        client_id: str,
        client: XUIClient,
    ) -> XUIAPIResponse[Any]:
        payload = XUIUpdateClientRequest.from_client(inbound_id, client)
        response = await self._request(
            "POST",
            f"panel/api/inbounds/updateClient/{client_id}",
            json=payload.model_dump(mode="json"),
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
        response = await self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")
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
        response = await self._request("POST", f"panel/api/inbounds/clientIps/{email}")
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[Any].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch client IPs.")
            payload = wrapper.obj
        else:
            payload = response
        return self._normalize_client_ips(payload)

    async def clear_client_ips(self, email: str) -> XUIAPIResponse[Any]:
        response = await self._request("POST", f"panel/api/inbounds/clearClientIps/{email}")
        api_response = XUIAPIResponse[Any].model_validate(response or {"success": True, "obj": None})
        if api_response.success is False:
            raise XUIRequestError(api_response.msg or "Failed to clear client IPs.")
        return api_response

    async def get_panel_settings(self) -> dict[str, Any]:
        response = await self._request("POST", "panel/setting/all")
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[dict[str, Any]].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch panel settings.")
            return wrapper.obj or {}
        return {}

    async def get_db_backup(self) -> bytes:
        """Download X-UI panel database backup, trying known endpoints."""
        if not self._authenticated:
            await self.login()

        endpoints = [
            "panel/api/server/getDb", 
            "panel/setting/getDb", 
            "server/getDb", 
            "xui/API/inbounds/getDb"
        ]
        last_error = ""

        for endpoint in endpoints:
            response = await self._client.request("GET", endpoint)
            if response.status_code in {401, 403}:
                self._authenticated = False
                await self.login()
                response = await self._client.request("GET", endpoint)

            if response.status_code == 200 and len(response.content) >= 100:
                return response.content
            else:
                last_error = f"HTTP {response.status_code}, len={len(response.content)}"

        raise XUIRequestError(f"Failed to download X-UI DB from any endpoint. Last response: {last_error}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any] | None:
        if not self._authenticated:
            await self.login()

        response = await self._send(method, path, **kwargs)
        if response.status_code in {401, 403}:
            self._authenticated = False
            await self.login()
            response = await self._send(method, path, **kwargs)
        return self._decode_response(response)

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        import asyncio
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise XUIRequestError(f"Timed out while calling X-UI endpoint '{path}'.") from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise XUIRequestError(
                    f"X-UI request to '{path}' failed with status {exc.response.status_code}: "
                    f"{self._safe_response_text(exc.response)}"
                ) from exc
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
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
        payload = response.json()
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
