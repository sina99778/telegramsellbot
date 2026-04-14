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


class SanaeiXUIClient:
    def __init__(self, config: XUIClientConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            follow_redirects=True,
            verify=False,
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
        payload = XUILoginRequest(
            username=self._config.username,
            password=self._config.password.get_secret_value(),
        )
        response = await self._send("POST", "login", json=payload.model_dump(mode="json"))
        data = self._decode_response(response)
        login_response = XUILoginResponse.model_validate(data or {"success": True})
        if login_response.success is False or not self._client.cookies:
            raise XUIAuthenticationError(login_response.msg or "X-UI authentication failed.")
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

    async def get_client_traffic(self, email: str) -> XUIClientTraffic:
        response = await self._request("GET", f"panel/api/inbounds/getClientTraffics/{email}")
        if isinstance(response, dict) and "obj" in response:
            wrapper = XUIAPIResponse[dict[str, Any] | list[dict[str, Any]]].model_validate(response)
            if wrapper.success is False:
                raise XUIRequestError(wrapper.msg or "Failed to fetch traffic.")
            payload = wrapper.obj
        else:
            payload = response

        if isinstance(payload, list):
            if not payload:
                raise XUIRequestError(f"No traffic stats found for client '{email}'.")
            payload = payload[0]
        if not isinstance(payload, dict):
            raise XUIRequestError("Unexpected traffic payload returned by X-UI.")
        return XUIClientTraffic.model_validate(payload)

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
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.TimeoutException as exc:
            raise XUIRequestError(f"Timed out while calling X-UI endpoint '{path}'.") from exc
        except httpx.HTTPStatusError as exc:
            raise XUIRequestError(
                f"X-UI request to '{path}' failed with status {exc.response.status_code}: "
                f"{self._safe_response_text(exc.response)}"
            ) from exc
        except httpx.RequestError as exc:
            cause = f" (caused by {type(exc.__cause__).__name__}: {exc.__cause__})" if exc.__cause__ else ""
            raise XUIRequestError(
                f"{type(exc).__name__} while calling X-UI endpoint '{path}'{cause}"
            ) from exc

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
