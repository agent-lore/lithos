"""Finding and system-stats MCP tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import invalid_input_envelope
from lithos.events import FINDING_POSTED, LithosEvent
from lithos.frontmatter_codec import normalize_datetime
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the finding/stats tools. See the late-binding rule in
    :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_finding_post(
        task_id: str,
        agent: str,
        summary: str,
        knowledge_id: str | None = None,
    ) -> dict[str, str]:
        """Post a finding to a task.

        Args:
            task_id: Task ID
            agent: Agent posting the finding
            summary: Finding summary
            knowledge_id: Optional linked knowledge document ID

        Returns:
            Dict with finding_id
        """
        logger.info("lithos_finding_post task=%s agent=%s", task_id, agent)
        span = get_current_span()
        span.set_attribute("lithos.agent", agent)
        span.set_attribute("lithos.task_id", task_id)
        finding_id = await server.coordination.post_finding(
            task_id=task_id,
            agent=agent,
            summary=summary,
            knowledge_id=knowledge_id,
        )
        span.set_attribute("lithos.finding_id", finding_id)

        payload: dict[str, str | int | float | bool | None] = {
            "finding_id": finding_id,
            "task_id": task_id,
            "agent": agent,
        }
        if knowledge_id is not None:
            payload["knowledge_id"] = knowledge_id

        await server._emit(
            LithosEvent(
                type=FINDING_POSTED,
                agent=agent,
                payload=payload,
            )
        )

        return {"finding_id": finding_id}

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_finding_list(
        task_id: str,
        since: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """List findings for a task.

        Args:
            task_id: Task ID
            since: Filter by created since (ISO datetime)

        Returns:
            Dict with findings list
        """
        logger.info("lithos_finding_list task=%s", task_id)
        span = get_current_span()
        span.set_attribute("lithos.task_id", task_id)

        since_dt = None
        if since:
            try:
                since_dt = normalize_datetime(datetime.fromisoformat(since))
            except ValueError:
                return invalid_input_envelope(f"Invalid since datetime: {since}")

        findings = await server.coordination.list_findings(
            task_id=task_id,
            since=since_dt,
        )

        span.set_attribute("lithos.result_count", len(findings))
        return {
            "findings": [
                {
                    "id": f.id,
                    "agent": f.agent,
                    "summary": f.summary,
                    "knowledge_id": f.knowledge_id,
                    "created_at": (f.created_at.isoformat() if f.created_at else None),
                }
                for f in findings
            ]
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_stats() -> dict[str, Any]:
        """Get knowledge base statistics and health indicators.

        Returns:
            Dict with document counts, search index stats, coordination
            stats, and health signals (index drift, broken links, expired
            claims, last-updated timestamps).
        """
        logger.info("lithos_stats")

        # Get document count (from in-memory cache — always available)
        total_docs = server.knowledge.document_count

        # Semantic chunk count via the public surface (returns 0 when
        # the Chroma store is quarantined).
        chroma_chunk_count: int = server.search.count_chunks()

        # Full-text document count with None-sentinel on failure.
        # NOTE: this deliberately diverges from _safe_tantivy_count(),
        # which returns *0* on error (used by OTEL gauge probes where a
        # numeric value is always required).  Here we return *None* so
        # callers can distinguish "index not yet written / unavailable"
        # from "zero documents indexed" — a meaningful difference for
        # drift detection and health reporting.  If you need the 0-on-
        # error behaviour, use _safe_tantivy_count() instead.
        tantivy_doc_count: int | None
        try:
            tantivy_doc_count = server.search.count_documents()
        except Exception:
            tantivy_doc_count = None

        # Index drift: knowledge corpus vs Tantivy index
        index_drift_detected = tantivy_doc_count is not None and tantivy_doc_count != total_docs

        # Get coordination stats
        coord_stats = await server.coordination.get_stats()

        # Update cached fields for synchronous OTEL gauge callbacks
        server._cached_active_claims = coord_stats.get("open_claims", 0)
        server._cached_agent_count = coord_stats.get("agents", 0)

        # Get tag count
        tags = await server.knowledge.get_all_tags()

        # Unresolved wiki-links: nodes in the graph that have no
        # matching document (represented as __unresolved__ placeholders)
        graph_stats = server.graph.get_stats()
        unresolved_links: int = graph_stats.get("unresolved_links", 0)

        # Stale (expired) documents
        expired_docs = server.knowledge.stale_document_count

        # Index last-updated timestamps from filesystem mtime
        def _dir_mtime(path: Path) -> str | None:
            try:
                mtime = path.stat().st_mtime
                return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
            except OSError:
                return None

        tantivy_last_updated = _dir_mtime(server._config.storage.tantivy_path)
        chroma_last_updated = _dir_mtime(server._config.storage.chroma_path)

        return {
            # Core counts
            "documents": total_docs,
            "chroma_chunk_count": chroma_chunk_count,
            "agents": coord_stats.get("agents", 0),
            "active_tasks": coord_stats.get("active_tasks", 0),
            "open_claims": coord_stats.get("open_claims", 0),
            "tags": len(tags),
            "duplicate_urls": server.knowledge.duplicate_url_count,
            # Health indicators
            "index_drift_detected": index_drift_detected,
            "tantivy_doc_count": tantivy_doc_count,
            "unresolved_links": unresolved_links,
            "expired_docs": expired_docs,
            "expired_claims": coord_stats.get("expired_claims", 0),
            "tantivy_last_updated": tantivy_last_updated,
            "chroma_last_updated": chroma_last_updated,
            # Graph stats
            "graph_node_count": graph_stats.get("nodes", 0),
            # graph_edge_count reflects ALL edges in the NetworkX graph,
            # including edges that point to __unresolved__ placeholder
            # nodes (i.e. wiki-links whose target document does not yet
            # exist).  It therefore equals resolved_edges + unresolved_links,
            # not just resolved edges.  Compare with unresolved_links above
            # to infer the resolved-only edge count if needed.
            "graph_edge_count": graph_stats.get("edges", 0),
        }
