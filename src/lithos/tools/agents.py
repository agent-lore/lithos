"""Agent-registry MCP tools."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import invalid_input_envelope
from lithos.events import AGENT_REGISTERED, LithosEvent
from lithos.knowledge import _normalize_datetime
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the agent-registry tools. See the late-binding rule in
    :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_agent_register(
        id: str,
        name: str | None = None,
        type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """Register or update an agent.

        Args:
            id: Agent identifier
            name: Human-friendly display name
            type: Agent type (e.g., "agent-zero", "claude-code")
            metadata: Optional extra info

        Returns:
            Dict with success and created booleans
        """
        logger.info("lithos_agent_register id=%s type=%s", id, type)
        span = get_current_span()
        span.set_attribute("lithos.agent.id", id)
        success, created = await server.coordination.register_agent(
            agent_id=id,
            name=name,
            agent_type=type,
            metadata=metadata,
        )
        span.set_attribute("lithos.created", created)

        if success:
            await server._emit(
                LithosEvent(
                    type=AGENT_REGISTERED,
                    agent=id,
                    payload={"agent_id": id, "name": name or ""},
                )
            )

        return {"success": success, "created": created}

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_agent_info(
        id: str,
    ) -> dict[str, Any] | None:
        """Get agent information.

        Args:
            id: Agent identifier

        Returns:
            Agent info dict or None if not found
        """
        logger.info("lithos_agent_info id=%s", id)
        span = get_current_span()
        span.set_attribute("lithos.agent.id", id)
        agent = await server.coordination.get_agent(id)
        if not agent:
            return None

        return {
            "id": agent.id,
            "name": agent.name,
            "type": agent.type,
            "first_seen_at": (agent.first_seen_at.isoformat() if agent.first_seen_at else None),
            "last_seen_at": (agent.last_seen_at.isoformat() if agent.last_seen_at else None),
            "metadata": agent.metadata,
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_agent_list(
        type: str | None = None,
        active_since: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """List all known agents.

        Args:
            type: Filter by agent type
            active_since: Filter by last activity (ISO datetime)

        Returns:
            Dict with agents list
        """
        logger.info("lithos_agent_list type=%s", type)
        span = get_current_span()

        since_dt = None
        if active_since:
            try:
                since_dt = _normalize_datetime(datetime.fromisoformat(active_since))
            except ValueError:
                return invalid_input_envelope(f"Invalid active_since datetime: {active_since}")

        agents = await server.coordination.list_agents(
            agent_type=type,
            active_since=since_dt,
        )

        span.set_attribute("lithos.result_count", len(agents))
        return {
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type,
                    "last_seen_at": (a.last_seen_at.isoformat() if a.last_seen_at else None),
                }
                for a in agents
            ]
        }
