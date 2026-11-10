"""Initialized MCP connections for local agents and authenticated remote agents."""
from __future__ import annotations

from contextlib import asynccontextmanager
import ipaddress
import os
from urllib.parse import urlsplit


def validate_mcp_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("MCP URL must be HTTPS without credentials, query or fragment")
    parts.port  # Reject malformed port values before opening a connection.
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if not loopback:
            raise ValueError("plain HTTP MCP connections are restricted to loopback hosts")
    return url


@asynccontextmanager
async def connect_mcp(*, command=None, url=None, api_key=None, timeout=30):
    """Yield an initialized session with HTTP-client redirect following disabled.

    The SDK may handle same-origin transport redirects; cross-origin and HTTPS
    downgrade redirects must not receive credentials.
    """
    from mcp import ClientSession

    if bool(command) == bool(url):
        raise ValueError("choose one MCP URL or server command")
    if url:
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as client:
            async with streamable_http_client(validate_mcp_url(url), http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    else:
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
