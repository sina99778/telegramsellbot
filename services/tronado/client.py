from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from schemas.internal.tronado import (
    TronadoConvertTomanRequest,
    TronadoConvertTomanResponse,
    TronadoCreateOrderRequest,
    TronadoCreateOrderResponse,
    TronadoStatusRequest,
    TronadoStatusResponse,
)

logger = logging.getLogger(__name__)


class TronadoRequestError(Exception):
    """Raised when the Tronado API returns an error or is unreachable."""


@dataclass(slots=True, frozen=True)
class TronadoClientConfig:
    api_key: str
    base_url: str
    timeout: float = 20.0


class TronadoClient:
    def __init__(self, config: TronadoClientConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.timeout, connect=10.0),
            headers={
                "x-api-key": config.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "TronadoClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def convert_toman_to_tron(self, *, toman: int, wallet: str) -> TronadoConvertTomanResponse:
        data = await self._request(
            "POST",
            "Toman/ConvertToTronWageSubtracted",
            json=TronadoConvertTomanRequest(Toman=toman, Wallet=wallet).model_dump(mode="json"),
        )
        return TronadoConvertTomanResponse.model_validate(data)

    async def create_order(
        self,
        *,
        payment_id: str,
        wallet_address: str,
        tron_amount: Decimal,
        callback_url: str,
        wage_from_business_percentage: int = 0,
    ) -> TronadoCreateOrderResponse:
        payload = TronadoCreateOrderRequest(
            PaymentID=payment_id,
            WalletAddress=wallet_address,
            TronAmount=tron_amount,
            CallbackUrl=callback_url,
            wageFromBusinessPercentage=wage_from_business_percentage,
        )
        data = await self._request("POST", "api/v3/GetOrderToken", json=payload.model_dump(mode="json"))
        response = TronadoCreateOrderResponse.model_validate(data)
        if not response.IsSuccessful or response.Data is None or not response.Data.Token:
            message = response.Message or (response.Data.ErrorMessage if response.Data else None) or str(data)
            raise TronadoRequestError(f"Tronado create order failed: {message}")
        return response

    async def get_status_by_payment_id(self, payment_id: str) -> TronadoStatusResponse:
        data = await self._request(
            "POST",
            "Order/GetStatusByPaymentID",
            json=TronadoStatusRequest(Id=payment_id).model_dump(mode="json"),
        )
        return TronadoStatusResponse.model_validate(data)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TronadoRequestError(f"Timed out while calling Tronado endpoint '{path}'.") from exc
        except httpx.HTTPStatusError as exc:
            raise TronadoRequestError(
                f"Tronado request to '{path}' failed with status {exc.response.status_code}: "
                f"{self._safe_response_text(exc.response)}"
            ) from exc
        except httpx.RequestError as exc:
            raise TronadoRequestError(f"Network error while calling Tronado endpoint '{path}': {exc}") from exc

        if "application/json" not in response.headers.get("content-type", ""):
            raise TronadoRequestError(
                f"Unexpected Tronado content type '{response.headers.get('content-type')}'."
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise TronadoRequestError("Unexpected Tronado response payload type.")
        if payload.get("Error"):
            raise TronadoRequestError(str(payload["Error"]))
        return payload

    @staticmethod
    def _safe_response_text(response: httpx.Response) -> str:
        return response.text[:500].strip() or "<empty response>"
