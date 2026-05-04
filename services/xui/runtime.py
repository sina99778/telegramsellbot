from __future__ import annotations

import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from pydantic import SecretStr

from core.security import decrypt_secret
from models.xui import XUIInboundRecord, XUIServerRecord
from services.xui.client import SanaeiXUIClient, XUIClientConfig


def build_sub_link(server: XUIServerRecord, sub_id: str) -> str:
    """
    Build the X-UI subscription URL.

    X-UI admin panel and subscription service run on DIFFERENT ports.
    base_url is the admin panel URL (e.g. http://1.2.3.4:54321/path).
    Subscription service uses server.subscription_port (default 2096).

    If server.sub_domain is set, use it instead of extracting from base_url.
    sub_domain can include scheme: "http://sub.example.com" or "https://sub.example.com"

    IMPORTANT: X-UI Sanaei subscription service runs on HTTP by default.
    The sub link scheme should NOT be inherited from the admin panel URL
    (which may be HTTPS). Default is always 'http' unless sub_domain
    explicitly specifies 'https://'.

    Result: <scheme>://<host>:<port>/sub/<sub_id>
    """
    scheme = None
    if server.sub_domain:
        scheme, host = _split_optional_scheme(server.sub_domain)
    else:
        host = _extract_host(server.base_url)

    # Default to http for X-UI subscription service
    # Only use https if sub_domain explicitly specifies it
    if scheme not in {"http", "https"}:
        scheme = "http"

    sub_port = server.subscription_port
    return f"{scheme}://{host}:{sub_port}/sub/{sub_id}"


def build_vless_uri(
    *,
    client_uuid: str,
    server: XUIServerRecord,
    inbound: XUIInboundRecord,
    sub_id: str,
    remark: str = "VPN",
) -> str:
    """
    Build a VLESS/VMess URI by reading actual stream settings from inbound metadata.
    Supports: tcp, ws, grpc, http, kcp networks and none/tls/reality security.
    """
    if server.config_domain:
        host = server.config_domain
    else:
        host = _extract_host(server.base_url)

    port = inbound.port or 443
    protocol = (inbound.protocol or "vless").lower()

    # Read stream settings from inbound metadata
    meta = inbound.metadata_ or {}
    stream = meta.get("stream_settings", {})
    if isinstance(stream, str):
        import json
        try:
            stream = json.loads(stream)
        except Exception:
            stream = {}

    network = stream.get("network", "tcp")
    security = stream.get("security", "none")

    # Build query parameters from actual stream settings
    params: dict[str, str] = {
        "type": network,
        "security": security,
    }

    # --- Network-specific settings ---
    if network == "ws":
        ws_settings = stream.get("wsSettings", {})
        path = ws_settings.get("path", "/")
        params["path"] = path
        ws_headers = ws_settings.get("headers", {})
        ws_host = (
            ws_headers.get("Host")
            or ws_headers.get("host")
            or ws_settings.get("host", "")  # some X-UI versions
        )
        if ws_host:
            params["host"] = ws_host
    elif network == "grpc":
        grpc_settings = stream.get("grpcSettings", {})
        service_name = grpc_settings.get("serviceName", "")
        if service_name:
            params["serviceName"] = service_name
    elif network == "tcp":
        tcp_settings = stream.get("tcpSettings", {})
        header = tcp_settings.get("header", {})
        header_type = header.get("type", "none")
        if header_type != "none":
            params["headerType"] = header_type
    elif network == "kcp":
        kcp_settings = stream.get("kcpSettings", {})
        header = kcp_settings.get("header", {})
        header_type = header.get("type", "none")
        if header_type != "none":
            params["headerType"] = header_type
        seed = kcp_settings.get("seed", "")
        if seed:
            params["seed"] = seed
    elif network in ("http", "h2"):
        http_settings = stream.get("httpSettings", {})
        path = http_settings.get("path", "/")
        params["path"] = path
        h_host = http_settings.get("host", [])
        if h_host and isinstance(h_host, list) and h_host[0]:
            params["host"] = h_host[0]

    # --- Security-specific settings ---
    if security == "tls":
        tls_settings = stream.get("tlsSettings", {})
        sni = tls_settings.get("serverName", "")
        if sni:
            params["sni"] = sni
        fp = tls_settings.get("fingerprint", "")
        if fp:
            params["fp"] = fp
        alpn = tls_settings.get("alpn", [])
        if alpn and isinstance(alpn, list):
            params["alpn"] = ",".join(alpn)
    elif security == "reality":
        reality_settings = stream.get("realitySettings", {})
        pbk = reality_settings.get("publicKey", "")
        sid = reality_settings.get("shortId", "")
        sni = reality_settings.get("serverName", "")
        fp = reality_settings.get("fingerprint", "")
        spx = reality_settings.get("spiderX", "")
        if pbk:
            params["pbk"] = pbk
        if sid:
            params["sid"] = sid
        if sni:
            params["sni"] = sni
        if fp:
            params["fp"] = fp
        if spx:
            params["spx"] = spx

    # --- External proxy / SNI override ---
    ext_proxy = stream.get("externalProxy", [])
    if ext_proxy and isinstance(ext_proxy, list) and len(ext_proxy) > 0:
        first_proxy = ext_proxy[0]
        if isinstance(first_proxy, dict):
            ext_dest = first_proxy.get("dest", "")
            ext_port = first_proxy.get("port", None)
            if ext_dest:
                # External proxy dest is the actual address clients connect to
                host = ext_dest.split(":")[0] if ":" in ext_dest else ext_dest
            if ext_port:
                port = int(ext_port)

            # If "host" param was NOT set (e.g. wsSettings.headers.Host is empty),
            # but we're using external proxy, the WS Host should be the original
            # server address (not the CDN/proxy address).
            if "host" not in params and network == "ws":
                # Use config_domain or extract from base_url as the WS Host
                original_host = server.config_domain or _extract_host(server.base_url)
                if original_host:
                    params["host"] = original_host

            if "sni" not in params:
                params["sni"] = host

    # Build URI
    from urllib.parse import urlencode, quote
    query = urlencode(params, safe="/:@,")

    if protocol == "vless":
        return f"vless://{client_uuid}@{host}:{port}?{query}#{quote(remark)}"
    elif protocol == "vmess":
        import base64, json as json_mod
        payload = {
            "v": "2",
            "ps": remark,
            "add": host,
            "port": str(port),
            "id": client_uuid,
            "aid": "0",
            "net": network,
            "type": params.get("headerType", "none"),
            "host": params.get("host", params.get("sni", "")),
            "path": params.get("path", ""),
            "tls": security if security != "none" else "",
            "sni": params.get("sni", ""),
            "fp": params.get("fp", ""),
            "alpn": params.get("alpn", ""),
        }
        encoded = base64.b64encode(json_mod.dumps(payload, separators=(",", ":")).encode()).decode()
        return f"vmess://{encoded}"
    else:
        return f"vless://{client_uuid}@{host}:{port}?{query}#{quote(remark)}"


def _extract_host(base_url: str) -> str:
    """
    Extract bare host (no port, no path, no scheme) from a URL.
    http://1.2.3.4:54321/xui  →  1.2.3.4
    http://example.com:8080   →  example.com
    """
    # Strip scheme
    url = re.sub(r"^https?://", "", base_url.strip())
    # Take only the host:port part (before first /)
    host_port = url.split("/")[0]
    # Strip port
    host = host_port.split(":")[0]
    return host


def _split_optional_scheme(value: str) -> tuple[str | None, str]:
    raw = value.strip()
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        return parsed.scheme.lower(), parsed.hostname or _extract_host(raw)
    return None, raw.split("/")[0].split(":")[0]


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
