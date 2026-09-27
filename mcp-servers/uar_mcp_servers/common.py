"""Shared startup for first-party MCP servers: stdio by default, Streamable HTTP with --http."""
from __future__ import annotations

import argparse
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings


def serve(server: MCPServer) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default=os.environ.get("UAR_MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("UAR_MCP_PORT", "8765")))
    a = ap.parse_args()
    if not a.http:
        server.run("stdio")
        return
    hosts = [h for h in os.environ.get("UAR_MCP_ALLOWED_HOSTS", f"127.0.0.1:{a.port},localhost:{a.port}").split(",") if h]
    server.run("streamable-http", host=a.host, port=a.port, stateless_http=True,
               transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts))
