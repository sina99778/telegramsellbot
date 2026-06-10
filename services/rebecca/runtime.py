"""Rebecca runtime helpers — mirror services/pasarguard/runtime.py."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import SecretStr

from core.config import settings
from core.security import decrypt_secret
from models.xui import XUIServerRecord
from schemas.internal.pasarguard import make_absolute_sub_url
from services.rebecca.client import RebeccaClient, RebeccaClientConfig


def build_rebecca_client_config(server: XUIServerRecord) -> RebeccaClientConfig:
    if server.credentials is None:
        raise ValueError("Rebecca server credentials are missing.")
    return RebeccaClientConfig(
        base_url=server.base_url,
        username=server.credentials.username,
        password=SecretStr(decrypt_secret(server.credentials.password_encrypted)),
        verify_ssl=settings.rebecca_verify_ssl,
    )


@asynccontextmanager
async def create_rebecca_client_for_server(server: XUIServerRecord) -> AsyncIterator[RebeccaClient]:
    async with RebeccaClient(build_rebecca_client_config(server)) as client:
        yield client


def absolute_sub_url(server: XUIServerRecord, subscription_url: str | None) -> str:
    return make_absolute_sub_url(server.base_url, subscription_url)
