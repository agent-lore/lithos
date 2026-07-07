"""MCP tool handlers, grouped by domain.

Each domain module exposes ``register(mcp, server) -> None`` which attaches
its handlers to the FastMCP instance as decorated closures over ``server``.

**Late-binding rule (load-bearing):** ``server.search`` / ``server.intake`` /
``server.memory`` / ``server.projection`` are ``None`` until
``await server.initialize()`` completes, while registration happens in
``LithosServer.__init__``. Handlers therefore close over ``server`` only and
read ``server.<component>`` at call time — never capture a component at
``register()`` scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP

from lithos.tools import agents, findings_stats, memory_edges, notes, read_search

if TYPE_CHECKING:
    from lithos.server import LithosServer


def register_all(mcp: FastMCP, server: LithosServer) -> None:
    """Register every extracted tool domain, in a fixed order."""
    agents.register(mcp, server)
    memory_edges.register(mcp, server)
    findings_stats.register(mcp, server)
    notes.register(mcp, server)
    read_search.register(mcp, server)
