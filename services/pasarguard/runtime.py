"""
PasarGuard runtime helpers — the panel-specific analogue of
services/xui/runtime.py. Builds a client from an XUIServerRecord (reused as the
generic "panel server" row) and resolves subscription URLs.

There is intentionally NO build_vless_uri() analogue: PasarGuard returns a
ready subscription_url, so we never synthesise per-inbound URIs ourselves.
"""
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
from services.pasarguard.client import PasarGuardClient, PasarGuardClientConfig


def build_pasarguard_client_config(server: XUIServerRecord) -> PasarGuardClientConfig:
    if server.credentials is None:
        raise ValueError("PasarGuard server credentials are missing.")
    return PasarGuardClientConfig(
        base_url=server.base_url,
        username=server.credentials.username,
        password=SecretStr(decrypt_secret(server.credentials.password_encrypted)),
        verify_ssl=settings.pasarguard_verify_ssl,
    )


@dataclass
class PooledPasarGuardClient:
    config: PasarGuardClientConfig
    client: PasarGuardClient

_pg_clients_pool: dict[UUID, PooledPasarGuardClient] = {}


@asynccontextmanager
async def create_pasarguard_client_for_server(
    server: XUIServerRecord,
) -> AsyncIterator[PasarGuardClient]:
    config = build_pasarguard_client_config(server)
    pooled = _pg_clients_pool.get(server.id)
    
    if pooled is None or pooled.config != config:
        if pooled is not None:
            await pooled.client.aclose()
            
        new_client = PasarGuardClient(config)
        _pg_clients_pool[server.id] = PooledPasarGuardClient(config=config, client=new_client)
        pooled = _pg_clients_pool[server.id]

    yield pooled.client


def absolute_sub_url(server: XUIServerRecord, subscription_url: str | None) -> str:
    """Resolve a (possibly relative) PasarGuard subscription URL against the
    server's base URL."""
    return make_absolute_sub_url(server.base_url, subscription_url)
