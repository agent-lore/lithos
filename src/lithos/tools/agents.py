"""Agent-registry MCP tools."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import invalid_input_envelope
from lithos.events import AGENT_ARCHIVED, AGENT_REGISTERED, LithosEvent
from lithos.frontmatter_codec import normalize_datetime
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
    ) -> dict[str, Any]:
        """Register or update an agent.

        Re-registering an existing id is idempotent (``created: false``) and
        un-archives it. Registering a *new* id whose ``name`` is already used
        by another active agent still succeeds but adds a ``warnings`` entry
        naming the existing agent — usually a sign the caller already has an
        id and should reuse it.

        Args:
            id: Agent identifier
            name: Human-friendly display name
            type: Agent type (e.g., "agent-zero", "claude-code")
            metadata: Optional extra info

        Returns:
            Dict with success and created booleans, plus warnings (list)
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

        others = await server.coordination.find_agent_name_collisions(name, exclude_id=id)
        warnings = [
            f"name {name!r} is already used by agent {other.id!r} (type={other.type!r}) — "
            "if that is you, reuse that id; lithos_agent_archive retires duplicates"
            for other in others
        ]
        span.set_attribute("lithos.agent.name_collisions", len(warnings))

        return {"success": success, "created": created, "warnings": warnings}

    @mcp.tool()
    @tool_metrics()
    @tool_span(map_coordination_error=True)
    async def lithos_agent_archive(
        id: str,
        agent: str,
    ) -> dict[str, Any]:
        """Archive an agent: it keeps its history (tasks, claims, findings
        still attribute to it) and stays readable via lithos_agent_info, but
        drops out of lithos_agent_list until it is next active. Any write by
        the archived id — or re-registering it — un-archives it.

        Idempotent: archiving an already-archived agent succeeds and keeps
        the original archived_at.

        Args:
            id: Agent to archive
            agent: Agent performing the archive (attribution)

        Returns:
            Dict with success, id, archived_at and already_archived; or the
            canonical error envelope with code "agent_not_found"
        """
        logger.info("lithos_agent_archive id=%s agent=%s", id, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent.id", id)
        # The archiver's activity counts (and self-archive then works).
        await server.coordination.ensure_agent_known(agent)
        archived, newly = await server.coordination.archive_agent(id)
        archived_at = archived.archived_at.isoformat() if archived.archived_at else None
        span.set_attribute("lithos.created", newly)

        if newly:
            await server._emit(
                LithosEvent(
                    type=AGENT_ARCHIVED,
                    agent=agent,
                    payload={"agent_id": id, "archived_at": archived_at},
                )
            )

        return {
            "success": True,
            "id": id,
            "archived_at": archived_at,
            "already_archived": not newly,
        }

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
            "archived_at": (agent.archived_at.isoformat() if agent.archived_at else None),
            "metadata": agent.metadata,
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_agent_list(
        type: str | None = None,
        active_since: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """List known agents, most recently active first.

        Archived agents are hidden unless include_archived is set. Every row
        carries archived_at (null while active).

        Args:
            type: Filter by agent type
            active_since: Filter by last activity (ISO datetime)
            include_archived: Also list archived agents

        Returns:
            Dict with agents list
        """
        logger.info("lithos_agent_list type=%s", type)
        span = get_current_span()

        since_dt = None
        if active_since:
            try:
                since_dt = normalize_datetime(datetime.fromisoformat(active_since))
            except ValueError:
                return invalid_input_envelope(f"Invalid active_since datetime: {active_since}")

        agents = await server.coordination.list_agents(
            agent_type=type,
            active_since=since_dt,
            include_archived=include_archived,
        )

        span.set_attribute("lithos.result_count", len(agents))
        return {
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type,
                    "last_seen_at": (a.last_seen_at.isoformat() if a.last_seen_at else None),
                    "archived_at": (a.archived_at.isoformat() if a.archived_at else None),
                }
                for a in agents
            ]
        }
