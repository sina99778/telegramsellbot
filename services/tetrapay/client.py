from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from core.config import settings
from schemas.internal.tetrapay import (
    TetraPayCreateOrderRequest,
    TetraPayCreateOrderResponse,
    TetraPayVerifyRequest,
    TetraPayVerifyResponse,
)

logger = logging.getLogger(__name__)


class TetraPayRequestError(Exception):
    """Raised when the TetraPay API returns an error or is unreachable."""


@dataclass
class TetraPayClientConfig:
    api_key: str
    base_url: str
    timeout: float = 10.0


class TetraPayClient:
    def __init__(self, config: TetraPayClientConfig) -> None:
        self.config = config
        self._session: httpx.AsyncClient | None = None

    async def __aenter__(self) -> TetraPayClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            self._session = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=self.config.timeout,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                }
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    def _get_session(self) -> httpx.AsyncClient:
        if self._session is None:
            raise RuntimeError("TetraPayClient session is not started. Use 'async with' context manager.")
        return self._session

    async def create_order(
        self,
        hash_id: str,
        amount: int,
        description: str,
        email: str | None = None,
        mobile: str | None = None,
        callback_url: str | None = None,
    ) -> TetraPayCreateOrderResponse:
        session = self._get_session()
        url = "/create_order"
        
        request_obj = TetraPayCreateOrderRequest(
            ApiKey=self.config.api_key,
            Hash_id=hash_id,
            Amount=amount,
            Description=description,
            Email=email,
            Mobile=mobile,
            CallbackURL=callback_url or settings.tetrapay_callback_url,
        )

        try:
            request_data = request_obj.model_dump(exclude_none=True)
            logger.info("TetraPay create_order request: amount=%s, hash_id=%s, callback=%s", 
                       request_data.get('Amount'), request_data.get('Hash_id'), request_data.get('CallbackURL'))
            response = await session.post(url, json=request_data)
            
            # Read body BEFORE checking status — 422 responses contain error details
            try:
                data = response.json()
            except Exception:
                raw_text = response.text[:500]
                logger.error("TetraPay non-JSON response (create_order): HTTP %s, body=%s", response.status_code, raw_text)
                raise TetraPayRequestError(f"API Error: HTTP {response.status_code}, body: {raw_text}")

            logger.info("TetraPay create_order response (HTTP %s): %s", response.status_code, data)

            # Check HTTP status and API status
            if response.status_code != 200 or str(data.get("status")) != "100":
                logger.error("TetraPay API error (create_order): HTTP %s, response=%s", response.status_code, data)
                msg = data.get('message') or data.get('error') or data.get('detail') or str(data)
                raise TetraPayRequestError(f"API Error (HTTP {response.status_code}): {msg}")

            return TetraPayCreateOrderResponse.model_validate(data)
        except TetraPayRequestError:
            raise
        except httpx.HTTPError as exc:
            logger.error("TetraPay connection error (create_order): %s", exc)
            raise TetraPayRequestError(f"Connection Error: {exc}") from exc
        except Exception as exc:
            logger.error("Failed to parse TetraPay response (create_order): %s", exc, exc_info=True)
            raise TetraPayRequestError(f"Parse Error: {exc}") from exc

    async def verify_payment(self, authority: str) -> TetraPayVerifyResponse:
        session = self._get_session()
        url = "/verify"

        request_obj = TetraPayVerifyRequest(
            ApiKey=self.config.api_key,
            authority=authority,
        )

        try:
            logger.info("TetraPay verify_payment request: authority=%s", authority)
            response = await session.post(url, json=request_obj.model_dump(exclude_none=True))
            
            # Read body BEFORE checking status
            try:
                data = response.json()
            except Exception:
                raw_text = response.text[:500]
                logger.error("TetraPay non-JSON response (verify): HTTP %s, body=%s", response.status_code, raw_text)
                raise TetraPayRequestError(f"API Error: HTTP {response.status_code}, body: {raw_text}")

            logger.info("TetraPay verify_payment response (HTTP %s): %s", response.status_code, data)

            return TetraPayVerifyResponse.model_validate(data)
        except TetraPayRequestError:
            raise
        except httpx.HTTPError as exc:
            logger.error("TetraPay connection error (verify_payment): %s", exc)
            raise TetraPayRequestError(f"Connection Error: {exc}") from exc
        except Exception as exc:
            logger.error("Failed to parse TetraPay verify response: %s", exc, exc_info=True)
            raise TetraPayRequestError(f"Parse Error: {exc}") from exc
