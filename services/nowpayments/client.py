from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from schemas.internal.nowpayments import (
    NowPaymentsInvoiceResponse,
    NowPaymentsPaymentCreateRequest,
    NowPaymentsPaymentStatusResponse,
)


class NowPaymentsClientError(Exception):
    pass


class NowPaymentsRequestError(NowPaymentsClientError):
    pass


@dataclass(slots=True, frozen=True)
class NowPaymentsClientConfig:
    api_key: SecretStr
    base_url: str = "https://api.nowpayments.io/v1"
    timeout_seconds: float = 20.0


class NowPaymentsClient:
    def __init__(self, config: NowPaymentsClientConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
            headers={
                "x-api-key": config.api_key.get_secret_value(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "NowPaymentsClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_payment_invoice(
        self,
        payload: NowPaymentsPaymentCreateRequest,
    ) -> NowPaymentsInvoiceResponse:
        data = await self._request(
            "POST",
            "invoice",
            json=payload.model_dump(mode="json", exclude_none=True),
        )
        return NowPaymentsInvoiceResponse.model_validate(data)

    async def get_payment_status(self, payment_id: str | int) -> NowPaymentsPaymentStatusResponse:
        data = await self._request("GET", f"payment/{payment_id}")
        return NowPaymentsPaymentStatusResponse.model_validate(data)

    async def get_invoice_status(self, invoice_id: str | int) -> dict[str, Any]:
        return await self._request("GET", f"invoice/{invoice_id}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise NowPaymentsRequestError(f"Timed out while calling NOWPayments endpoint '{path}'.") from exc
        except httpx.HTTPStatusError as exc:
            raise NowPaymentsRequestError(
                f"NOWPayments request to '{path}' failed with status {exc.response.status_code}: "
                f"{self._safe_response_text(exc.response)}"
            ) from exc
        except httpx.RequestError as exc:
            raise NowPaymentsRequestError(
                f"Network error while calling NOWPayments endpoint '{path}': {exc}"
            ) from exc

        if "application/json" not in response.headers.get("content-type", ""):
            raise NowPaymentsRequestError(
                f"Unexpected NOWPayments content type '{response.headers.get('content-type')}'."
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise NowPaymentsRequestError("Unexpected NOWPayments response payload type.")
        return payload

    @staticmethod
    def _safe_response_text(response: httpx.Response) -> str:
        return response.text[:500].strip() or "<empty response>"
