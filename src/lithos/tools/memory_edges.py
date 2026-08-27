"""Cognitive-memory and asserted-edge MCP tools."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import error_envelope
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the memory/edge tools. See the late-binding rule in
    :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_edge_upsert(
        from_id: str,
        to_id: str,
        type: str,
        weight: float,
        namespace: str,
        provenance_actor: str | None = None,
        provenance_type: str | None = None,
        evidence: Any = None,
        conflict_state: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a typed edge in edges.db.

        Upsert key is (from_id, to_id, type, namespace).

        Args:
            from_id: Source node ID (an unambiguous >= 6-char prefix of an
                existing note resolves to its full id; other values are
                stored as given — node ids are free-form)
            to_id: Target node ID (same resolution as from_id)
            type: Edge type (e.g. 'derived_from', 'related_to')
            weight: Edge weight (float)
            namespace: Namespace for the edge (required)
            provenance_actor: Agent/process that created the edge
            provenance_type: How the edge was derived
            evidence: Supporting evidence (dict or list only, not scalars)
            conflict_state: Conflict state marker

        Returns:
            Status envelope with edge_id.
        """
        # Lenient reference resolution (task 83257ced): node ids are
        # free-form, but a display prefix of an existing note must never be
        # persisted as a phantom endpoint.
        from_id = server.knowledge.resolve_id_lenient(from_id)
        to_id = server.knowledge.resolve_id_lenient(to_id)
        logger.info("lithos_edge_upsert from=%s to=%s type=%s", from_id, to_id, type)

        if not namespace:
            return error_envelope("invalid_input", "namespace is required")

        # Validate evidence type
        if evidence is not None and not isinstance(evidence, (dict, list)):
            return error_envelope(
                "invalid_input",
                "evidence must be a dict, list, or null — scalars are not accepted",
            )

        evidence_str = json.dumps(evidence) if evidence is not None else None

        # The MCP surface carries no separate ``agent`` field for
        # this tool; the asserting party is ``provenance_actor`` (with
        # a tool-name fallback so ``ensure_agent_known`` always runs,
        # per ADR-0006 Slice 1 / issue #263).
        agent = provenance_actor or "lithos_edge_upsert"
        edge_id = await server.memory.edge_upsert(
            agent=agent,
            from_id=from_id,
            to_id=to_id,
            edge_type=type,
            weight=weight,
            namespace=namespace,
            provenance_actor=provenance_actor,
            provenance_type=provenance_type,
            evidence=evidence_str,
            conflict_state=conflict_state,
        )

        return {
            "status": "ok",
            "edge_id": edge_id,
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_edge_list(
        from_id: str | None = None,
        to_id: str | None = None,
        type: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Query edges from edges.db by optional filters.

        Args:
            from_id: Filter by source node ID (an unambiguous >= 6-char note
                prefix resolves to its full id; other values filter literally)
            to_id: Filter by target node ID (same resolution as from_id)
            type: Filter by edge type
            namespace: Filter by namespace

        Returns:
            Dict with results list of edge dicts.
        """
        if from_id is not None:
            from_id = server.knowledge.resolve_id_lenient(from_id)
        if to_id is not None:
            to_id = server.knowledge.resolve_id_lenient(to_id)
        logger.info("lithos_edge_list from=%s to=%s type=%s ns=%s", from_id, to_id, type, namespace)
        span = get_current_span()

        edges = await server.memory.edge_list(
            from_id=from_id,
            to_id=to_id,
            edge_type=type,
            namespace=namespace,
        )

        span.set_attribute("lithos.result_count", len(edges))
        return {"results": edges}

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_conflict_resolve(
        edge_id: str,
        resolution: str,
        resolver: str,
        winner_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a contradiction between two notes.

        Sets the resolution state on a contradicts edge so future retrieval
        reflects the resolution.

        Args:
            edge_id: The edge ID of the contradiction to resolve
            resolution: One of: accepted_dual, superseded, refuted, merged
            resolver: Agent or user identifier performing the resolution
            winner_id: Required when resolution is 'superseded'; must be
                either from_id or to_id of the edge (an unambiguous
                >= 6-char note prefix resolves to its full id)
        """
        if winner_id is not None:
            winner_id = server.knowledge.resolve_id_lenient(winner_id)
        return await server.memory.conflict_resolve(
            edge_id=edge_id,
            resolution=resolution,
            resolver=resolver,
            winner_id=winner_id,
        )

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_cache_lookup(
        query: str,
        source_url: str | None = None,
        max_age_hours: float | None = None,
        min_confidence: float = 0.5,
        limit: int = 3,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Check if fresh cached knowledge exists before doing expensive research.

        Returns a cache hit with full document content if fresh knowledge exists,
        a stale reference if expired knowledge exists (update instead of duplicate),
        or a clean miss if nothing relevant is found.

        Args:
            query: What you are about to research
            source_url: Canonical URL for exact dedup-aware lookup
            max_age_hours: Reject docs older than N hours (uses updated_at)
            min_confidence: Minimum confidence score threshold — candidates whose
                ``metadata.confidence`` is strictly below this value are skipped
                entirely (default: 0.5).
            limit: Max candidate docs to evaluate (default: 3).
            tags: Restrict to tagged docs (AND semantics)

        Returns:
            Dict with hit, document, stale_exists, stale_id
        """
        return await server.memory.cache_lookup(
            query=query,
            source_url=source_url,
            max_age_hours=max_age_hours,
            min_confidence=min_confidence,
            limit=limit,
            tags=tags,
        )
