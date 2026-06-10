"""Assay MCP server package — agent-facing tools over :class:`AssayService`.

Architecture §6: exposes the evaluate → library loop as Model Context Protocol
tools an LLM agent calls to drive alpha mining. The server object and CLI entry
point live in :mod:`assay.mcp.server`; this package re-exports them as the stable
surface (``python -m assay.mcp.server`` is the runnable entry point — §6.5).

Importing this package requires no MASSIVE credentials: the data store under the
service is built lazily, so only tools that touch price data need creds.
"""

from __future__ import annotations

# Re-export the server object as ``mcp`` (and its CLI entry point). We deliberately
# do NOT bind the name ``server`` here: that would shadow the ``assay.mcp.server``
# submodule on attribute access (``import assay.mcp.server`` would then resolve to
# the FastMCP instance, not the module). Use ``assay.mcp.mcp`` for the instance.
from assay.mcp.server import get_service, main, mcp

__all__ = ["mcp", "main", "get_service"]
