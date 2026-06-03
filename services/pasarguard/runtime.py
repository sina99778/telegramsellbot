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


@asynccontextmanager
async def create_pasarguard_client_for_server(
    server: XUIServerRecord,
) -> AsyncIterator[PasarGuardClient]:
    async with PasarGuardClient(build_pasarguard_client_config(server)) as client:
        yield client


def absolute_sub_url(server: XUIServerRecord, subscription_url: str | None) -> str:
    """Resolve a (possibly relative) PasarGuard subscription URL against the
    server's base URL."""
    return make_absolute_sub_url(server.base_url, subscription_url)
