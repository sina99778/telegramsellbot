"""Trust only the host gateway used by the loopback-bound nginx proxy."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

import uvicorn


def trusted_proxy_ips(route_table: str) -> str:
    peers = ["127.0.0.1", "::1"]
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            if int(fields[3], 16) & 2:
                gateway = ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1])
                if not gateway.is_unspecified:
                    peers.append(str(gateway))
        except ValueError:
            continue
    return ",".join(dict.fromkeys(peers))


if __name__ == "__main__":
    configured = os.environ.get("FORWARDED_ALLOW_IPS")
    if configured is None:
        try:
            configured = trusted_proxy_ips(Path("/proc/net/route").read_text())
        except OSError:
            configured = "127.0.0.1,::1"
    uvicorn.run(
        "apps.api.main:app",
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips=configured,
    )
