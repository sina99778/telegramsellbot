from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from pydantic import SecretStr

from core.security import decrypt_secret
from models.xui import XUIInboundRecord, XUIServerRecord
from services.xui.client import SanaeiXUIClient, XUIClientConfig


def build_sub_link(base_url: str, sub_id: str) -> str:
    return f"{base_url.rstrip('/')}/sub/{sub_id}"


def build_xui_client_config(server: XUIServerRecord) -> XUIClientConfig:
    if server.credentials is None:
        raise ValueError("X-UI server credentials are missing.")

    return XUIClientConfig(
        base_url=server.base_url,
        username=server.credentials.username,
        password=SecretStr(decrypt_secret(server.credentials.password_encrypted)),
    )


@asynccontextmanager
async def create_xui_client_for_server(server: XUIServerRecord) -> AsyncIterator[SanaeiXUIClient]:
    async with SanaeiXUIClient(build_xui_client_config(server)) as client:
        yield client


def ensure_inbound_server_loaded(inbound: XUIInboundRecord) -> XUIServerRecord:
    server = inbound.server
    if server is None:
        raise ValueError("Inbound server relation is missing.")
    if server.credentials is None:
        raise ValueError("Inbound server credentials relation is missing.")
    return server
