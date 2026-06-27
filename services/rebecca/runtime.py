"""Rebecca runtime helpers — mirror services/pasarguard/runtime.py."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

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


@dataclass
class PooledRebeccaClient:
    config: RebeccaClientConfig
    client: RebeccaClient

_rebecca_clients_pool: dict[UUID, PooledRebeccaClient] = {}


@asynccontextmanager
async def create_rebecca_client_for_server(server: XUIServerRecord) -> AsyncIterator[RebeccaClient]:
    config = build_rebecca_client_config(server)
    pooled = _rebecca_clients_pool.get(server.id)
    
    if pooled is None or pooled.config != config:
        if pooled is not None:
            await pooled.client.aclose()
            
        new_client = RebeccaClient(config)
        _rebecca_clients_pool[server.id] = PooledRebeccaClient(config=config, client=new_client)
        pooled = _rebecca_clients_pool[server.id]

    yield pooled.client


def absolute_sub_url(server: XUIServerRecord, subscription_url: str | None) -> str:
    return make_absolute_sub_url(server.base_url, subscription_url)
