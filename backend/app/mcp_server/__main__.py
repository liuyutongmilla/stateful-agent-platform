"""Run the admin tool server on its own: `python -m app.mcp_server`.

Serves MCP over streamable HTTP so the agent runtime can reach it with
`MCP_URL=http://<host>:8001/mcp`. In this split deployment set
SPICEDB_ENDPOINT too: the in-memory authorizer is per-process.
"""

from __future__ import annotations

import asyncio
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from ..authz import make_authorizer
from ..config import get_settings
from .server import build_admin_server


async def main() -> None:
    settings = get_settings()
    authz = await make_authorizer(settings)
    server = build_admin_server(authz, None, settings.secret)
    # Host-header allowlist (DNS-rebinding protection). Loopback always; add the
    # name other services use to reach this one, e.g. MCP_ALLOWED_HOSTS=mcp:8001
    extra = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *extra],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = server.mcp.streamable_http_app(transport_security=security)
    config = uvicorn.Config(
        app, host=os.environ.get("MCP_HOST", "0.0.0.0"), port=int(os.environ.get("MCP_PORT", "8001")), log_level="info"
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
