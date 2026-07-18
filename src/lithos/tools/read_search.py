"""Read, search, retrieval, and graph-neighbourhood MCP tools."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from lithos.envelopes import error_envelope, invalid_input_envelope
from lithos.errors import SearchBackendError
from lithos.frontmatter_codec import normalize_datetime, validate_metadata_match
from lithos.telemetry import get_current_span, tool_metrics
from lithos.tools._seam import tool_span

if TYPE_CHECKING:
    from lithos.server import LithosServer

logger = logging.getLogger(__name__)

# Practical upper bound for ``lithos_list(content_query=...)``. Tantivy
# returns up to this many hits before Python-side filters run. A million
# matches from a single FTS query would already be degenerate; at that
# point the caller should tighten the query, not ask for more results.
_CONTENT_QUERY_FTS_CAP = 1_000_000

_RELATED_INCLUDES = ("links", "provenance", "edges")


def register(mcp: FastMCP, server: LithosServer) -> None:
    """Register the read/search tools. See the late-binding rule in :mod:`lithos.tools`."""

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_read(
        id: str | None = None,
        path: str | None = None,
        max_length: int | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Read a knowledge file by ID or path.

        Args:
            id: UUID of knowledge item
            path: File path relative to knowledge/
            max_length: Truncate content to N characters
            agent_id: Caller identity for audit logging (optional)

        Returns:
            Dict with id, title, content, metadata, links, truncated,
            retrieval_count
        """
        logger.info("lithos_read id=%s path=%s", id, path)
        span = get_current_span()
        if id:
            span.set_attribute("lithos.id", id)

        try:
            doc, truncated = await server.knowledge.read(
                id=id,
                path=path,
                max_length=max_length,
            )
        except FileNotFoundError as e:
            return error_envelope("doc_not_found", str(e))

        # Audit log — awaited so the write is committed before we query
        # retrieval_count (avoids TOCTOU off-by-one). lithos_search uses
        # fire-and-forget (asyncio.create_task) for its batch write since
        # retrieval_count accuracy is not required there.
        audit_agent = agent_id or "unknown"
        await server.coordination.log_access(
            doc_id=doc.id,
            operation="read",
            agent_id=audit_agent,
        )

        # Retrieval count — how many times this doc has been read
        retrieval_count = await server.coordination.get_retrieval_count(doc.id)

        span.set_attribute("lithos.truncated", truncated)
        meta = doc.metadata.to_dict()
        meta["source_url"] = doc.metadata.source_url  # null when None
        meta.setdefault("derived_from_ids", [])
        # Free-form key/value metadata (#305) as an isolated dict, so
        # callers can read back exactly what they wrote via the
        # lithos_write `metadata` param without sifting reserved fields.
        meta["extra"] = dict(doc.metadata.extra)
        return {
            "id": doc.id,
            "title": doc.title,
            "content": doc.content,
            "metadata": meta,
            "links": [{"target": link.target, "display": link.display} for link in doc.links],
            "truncated": truncated,
            "retrieval_count": retrieval_count,
        }

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_search(
        query: str,
        limit: int = 10,
        mode: str = "hybrid",
        tags: list[str] | None = None,
        author: str | None = None,
        path_prefix: str | None = None,
        threshold: float | None = None,
        seed_ids: list[str] | None = None,
        graph_depth: int = 2,
        entities: list[str] | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Search across the knowledge base.

        Supports four search modes:
        - ``hybrid`` (default): Merges Tantivy BM25 full-text and ChromaDB
          cosine-similarity results using Reciprocal Rank Fusion (RRF, k=60).
          Best overall quality.
        - ``fulltext``: Pure Tantivy full-text search (BM25). Supports Tantivy
          query syntax (e.g. field-specific queries, boolean operators).
        - ``semantic``: Pure ChromaDB semantic/vector search. Finds documents
          with similar meaning even when keywords differ.
        - ``graph``: Wiki-link graph traversal. Discovers related documents by
          following links from seed documents up to *graph_depth* hops.
          Seeds are either provided via *seed_ids* or discovered automatically
          via a fast hybrid search on *query*.

        Args:
            query: Search query string
            limit: Max results (default: 10)
            mode: Search mode — "hybrid" | "fulltext" | "semantic" | "graph"
                  (default: "hybrid")
            tags: Filter by tags (AND) — fulltext/semantic/hybrid only
            author: Filter by author (fulltext/semantic/hybrid only)
            path_prefix: Filter by path prefix (fulltext/semantic/hybrid only)
            threshold: Minimum similarity 0-1 for semantic/hybrid (default: 0.5)
            seed_ids: Starting document IDs for graph mode.  If omitted,
                      seeds are discovered via hybrid search.
            graph_depth: BFS hop depth for graph mode (1-3, default: 2)
            entities: Filter results to documents whose ``entities``
                      frontmatter contains every named entity (exact
                      match, AND). Applies to all modes; resolved via an
                      inverted index and applied as a post-filter.
            agent_id: Caller identity for audit logging (optional)

        Returns:
            Dict with results list containing id, title, snippet, score, path,
            source_url, updated_at, is_stale, derived_from_ids
        """
        logger.info("lithos_search mode=%s query_len=%d limit=%d", mode, len(query), limit)
        span = get_current_span()
        span.set_attribute("lithos.query.length", len(query))
        span.set_attribute(
            "lithos.query.sha256",
            hashlib.sha256(query.encode()).hexdigest()[:16],
        )
        span.set_attribute("lithos.limit", limit)
        span.set_attribute("lithos.mode", mode)

        valid_modes = {"hybrid", "fulltext", "semantic", "graph"}
        if mode not in valid_modes:
            return error_envelope(
                "invalid_mode",
                f"Unknown search mode {mode!r}. Valid values: hybrid, fulltext, semantic, graph.",
            )

        # Entities are not a per-backend filter: resolve the candidate
        # set once from the knowledge inverted index (#316) and
        # post-filter every mode's hits. Over-fetch to compensate,
        # mirroring the engine's own post-filter heuristic.
        entity_candidates = server.knowledge.entities_candidate_ids(entities)
        if entity_candidates is not None and not entity_candidates:
            # No document carries every requested entity — skip the
            # backend search entirely.
            return {"results": []}
        fetch_limit = limit * 5 if entity_candidates is not None else limit

        def _build_result(r: Any, score_attr: str = "score") -> dict[str, Any]:
            return {
                "id": r.id,
                "title": r.title,
                "snippet": r.snippet,
                "score": getattr(r, score_attr),
                "path": r.path,
                "source_url": r.source_url,
                "updated_at": r.updated_at,
                "is_stale": r.is_stale,
                "derived_from_ids": server.knowledge.get_doc_sources(r.id),
            }

        # Thread safety note: SearchManager read methods (full_text_search, semantic_search,
        # hybrid_search) and the mutating methods (index, remove) are
        # all wrapped in asyncio.to_thread() so Tantivy commits and ChromaDB embedding
        # don't block the event loop. Concurrent read+write is not protected by a lock,
        # but tantivy-py and ChromaDB are thread-safe for these operations. The
        # embedding model is loaded eagerly in SearchEngine.create() at server
        # startup, so no model-init race is reachable here.
        if mode == "fulltext":
            ft_results = await asyncio.to_thread(
                server.search.full_text_search,
                query=query,
                limit=fetch_limit,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
            )
            results_payload = [_build_result(r) for r in ft_results]
        elif mode == "semantic":
            sem_results = await asyncio.to_thread(
                server.search.semantic_search,
                query=query,
                limit=fetch_limit,
                threshold=threshold,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
            )
            results_payload = [_build_result(r, score_attr="similarity") for r in sem_results]
        elif mode == "graph":
            graph_results = await asyncio.to_thread(
                server.search.graph_search,
                query=query,
                graph=server.graph,
                seed_ids=seed_ids,
                depth=graph_depth,
                limit=fetch_limit,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
                threshold=threshold,
            )
            results_payload = [_build_result(r) for r in graph_results]
        else:
            # hybrid (default)
            hybrid_results = await asyncio.to_thread(
                server.search.hybrid_search,
                query=query,
                limit=fetch_limit,
                threshold=threshold,
                tags=tags,
                author=author,
                path_prefix=path_prefix,
            )
            results_payload = [_build_result(r) for r in hybrid_results]

        if entity_candidates is not None:
            results_payload = [r for r in results_payload if r["id"] in entity_candidates][:limit]

        span.set_attribute("lithos.result_count", len(results_payload))
        logger.info("lithos_search mode=%s results=%d", mode, len(results_payload))

        # Audit log every returned document in a single batch write — fire-and-forget.
        # Using log_access_batch avoids N concurrent SQLite connections (previously
        # one asyncio.create_task per result document).
        audit_agent = agent_id or "unknown"
        asyncio.create_task(  # noqa: RUF006
            server.coordination.log_access_batch(
                doc_ids=[r["id"] for r in results_payload],
                operation="search_result",
                agent_id=audit_agent,
            )
        )

        return {"results": results_payload}

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_retrieve(
        query: str,
        limit: int = 10,
        namespace_filter: list[str] | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        surface_conflicts: bool = False,
        max_context_nodes: int | None = None,
        tags: list[str] | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, Any]:
        """LCMA cognitive retrieval — runs seven scouts with reranking.

        Orchestrates parallel scouts against the knowledge base, applies
        merge-and-normalize, Terrace 1 reranking, and writes an audit
        receipt on every call.

        Args:
            query: Search query string (required)
            limit: Max results (default: 10)
            namespace_filter: Restrict to these namespaces
            agent_id: Caller identity for access-scope gating and audit
            task_id: Task context — activates task_context scout and
                working-memory tracking
            surface_conflicts: Reserved for MVP 2 contradiction surfacing
            max_context_nodes: Provenance seed count (defaults to limit)
            tags: Filter by tags (AND semantics)
            path_prefix: Filter by path prefix

        Returns:
            Dict with results list (superset of lithos_search result
            schema), temperature, terrace_reached, receipt_id, and the
            degraded-mode signal ``degraded`` (bool) / ``failed_scouts`` (names
            of any scouts whose backend raised — empty when all scouts ran).
        """
        logger.info(
            "lithos_retrieve: called",
            extra={
                "query_len": len(query),
                "limit": limit,
                "agent_id": agent_id,
                "task_id": task_id,
                "namespace_filter": namespace_filter,
                "surface_conflicts": surface_conflicts,
            },
        )
        span = get_current_span()
        span.set_attribute("lithos.query.length", len(query))
        span.set_attribute("lithos.limit", limit)

        # Envelope-shaping short-circuit: when LCMA is disabled the
        # CognitiveMemory.retrieve precondition would fail, so we
        # surface a typed error response instead. The Module method
        # itself stays free of envelope concerns.
        if not server._config.lcma.enabled:
            logger.warning("lithos_retrieve: LCMA is disabled")
            return error_envelope("lcma_disabled", "LCMA is disabled via configuration")

        result = await server.memory.retrieve(
            query=query,
            limit=limit,
            namespace_filter=namespace_filter,
            agent_id=agent_id,
            task_id=task_id,
            surface_conflicts=surface_conflicts,
            max_context_nodes=max_context_nodes,
            tags=tags,
            path_prefix=path_prefix,
        )

        result_count = len(result.get("results", []))  # type: ignore[union-attr]
        span.set_attribute("lithos.result_count", result_count)
        logger.info(
            "lithos_retrieve: completed",
            extra={
                "result_count": result_count,
                "receipt_id": result.get("receipt_id"),  # type: ignore[union-attr]
                "temperature": result.get("temperature"),  # type: ignore[union-attr]
                "terrace_reached": result.get("terrace_reached"),  # type: ignore[union-attr]
                "degraded": result.get("degraded"),  # type: ignore[union-attr]
                "failed_scouts": result.get("failed_scouts"),  # type: ignore[union-attr]
                "agent_id": agent_id,
                "task_id": task_id,
            },
        )
        return result  # type: ignore[return-value]

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_list(
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        author: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
        title_contains: str | None = None,
        content_query: str | None = None,
        metadata_match: dict | None = None,
        entities: list[str] | None = None,
    ) -> dict[str, Any]:
        """List knowledge documents with filters.

        Args:
            path_prefix: Filter by path prefix
            tags: Filter by tags (AND)
            author: Filter by author
            since: Filter by updated since (ISO datetime)
            limit: Max results (default: 50)
            offset: Pagination offset
            title_contains: Filter by case-insensitive substring match on title
            entities: Filter by entity names from the document's ``entities``
                frontmatter (AND across the list, exact match). Resolved via
                an inverted index (no full scan).
            metadata_match: Filter by free-form metadata (AND across keys). For
                each ``key: q`` a document matches when its stored metadata
                value equals ``q`` or is a list containing ``q`` (so a note with
                ``github_repos: ["org/a","org/b"]`` matches
                ``{"github_repos": "org/a"}``). Query values must be scalars
                (string/number/boolean); type-sensitive. Resolved via an
                inverted index (no full scan).
            content_query: Filter by full-text search query (Tantivy).
                Tantivy-native filters (``tags``, ``author``,
                ``path_prefix``) are pushed down into the search query so
                ranking runs over the already-filtered candidate set,
                which is necessary for correctness under ranking pressure
                (see #194). ``since`` and ``title_contains`` are applied
                against the metadata cache after ranking.

        Returns:
            Dict with items list and total count
        """
        logger.info("lithos_list limit=%d offset=%d", limit, offset)
        span = get_current_span()
        span.set_attribute("lithos.limit", limit)

        # Normalize to UTC so the comparison against normalize_datetime'd
        # document timestamps below never mixes naive and aware values.
        since_dt = None
        if since:
            try:
                since_dt = normalize_datetime(datetime.fromisoformat(since))
            except ValueError:
                return invalid_input_envelope(f"Invalid since datetime: {since}")

        if metadata_match is not None:
            try:
                validate_metadata_match(metadata_match)
            except ValueError as e:
                return invalid_input_envelope(str(e))

        if content_query is not None:
            # Push the Tantivy-native filters into the search call so
            # ranking runs over the filtered candidate set. Using a
            # global ranked window and then intersecting (the prior
            # approach) silently dropped matches when filtered docs
            # ranked deep globally — see #194.
            try:
                fts_results = await asyncio.to_thread(
                    server.search.full_text_search,
                    query=content_query,
                    limit=_CONTENT_QUERY_FTS_CAP,
                    tags=tags,
                    author=author,
                    path_prefix=path_prefix,
                )
            except SearchBackendError as exc:
                return error_envelope("search_backend_error", f"Full-text search failed: {exc}")

            # Apply the filters Tantivy doesn't handle: ``since``,
            # ``title_contains`` and ``metadata_match``. Consult the
            # metadata cache / inverted index so we don't incur a disk
            # read per candidate. ``meta_candidates`` is None when no
            # metadata filter is requested.
            #
            # Like ``since``/``title_contains``, ``metadata_match`` is a
            # post-rank filter here: metadata isn't a Tantivy field, so
            # it can't be pushed into the query the way tags/author/
            # path_prefix are (#194). It runs against the full ranked
            # window (cap = _CONTENT_QUERY_FTS_CAP = 1e6), so a match is
            # only ever dropped if >1e6 docs match content_query — the
            # same bound the other two post-filters already accept.
            meta_candidates = server.knowledge.metadata_candidate_ids(metadata_match)
            entity_candidates = server.knowledge.entities_candidate_ids(entities)
            if (meta_candidates is not None and not meta_candidates) or (
                entity_candidates is not None and not entity_candidates
            ):
                # An equality filter matched nothing — no point walking
                # the ranked window.
                fts_results = []
            matching_ids: list[str] = []
            for r in fts_results:
                if meta_candidates is not None and r.id not in meta_candidates:
                    continue
                if entity_candidates is not None and r.id not in entity_candidates:
                    continue
                cached = server.knowledge.get_cached_meta(r.id)
                if cached is None:
                    continue
                if since_dt is not None:
                    cached_updated = normalize_datetime(cached.updated_at)
                    if cached_updated < since_dt:
                        continue
                if (
                    title_contains is not None
                    and title_contains.lower() not in (cached.title or "").lower()
                ):
                    continue
                matching_ids.append(r.id)

            total = len(matching_ids)
            page_ids = matching_ids[offset : offset + limit]
            docs = []
            for doc_id in page_ids:
                try:
                    doc, _ = await server.knowledge.read(id=doc_id)
                    docs.append(doc)
                except Exception:
                    # Cache can briefly lag the filesystem during
                    # reconcile; skipping mirrors list_all's behaviour.
                    continue
        elif title_contains is not None:
            # ``title_contains`` has no Tantivy-backed fast path; fall
            # back to list_all and filter in memory. Tracked in #201
            # for a metadata-cache-only variant.
            _, total_base = await server.knowledge.list_all(
                path_prefix=path_prefix,
                tags=tags,
                author=author,
                since=since_dt,
                metadata_match=metadata_match,
                entities=entities,
                limit=0,
                offset=0,
            )
            if total_base > 0:
                all_docs, _ = await server.knowledge.list_all(
                    path_prefix=path_prefix,
                    tags=tags,
                    author=author,
                    since=since_dt,
                    metadata_match=metadata_match,
                    entities=entities,
                    limit=total_base,
                    offset=0,
                )
            else:
                all_docs = []
            all_docs = [d for d in all_docs if title_contains.lower() in d.title.lower()]
            total = len(all_docs)
            docs = all_docs[offset : offset + limit]
        else:
            docs, total = await server.knowledge.list_all(
                path_prefix=path_prefix,
                tags=tags,
                author=author,
                since=since_dt,
                metadata_match=metadata_match,
                entities=entities,
                limit=limit,
                offset=offset,
            )

        span.set_attribute("lithos.result_count", len(docs))
        logger.info("lithos_list results=%d total=%d", len(docs), total)
        return {
            "items": [
                {
                    "id": d.id,
                    "title": d.title,
                    "path": str(d.path),
                    "updated": d.metadata.updated_at.isoformat(),
                    "tags": d.metadata.tags,
                    "source_url": d.metadata.source_url or "",
                    "derived_from_ids": server.knowledge.get_doc_sources(d.id),
                    "metadata": dict(d.metadata.extra),
                }
                for d in docs
            ],
            "total": total,
        }

    # ==================== Graph Tools ====================

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_related(
        id: str,
        include: list[str] | None = None,
        depth: int = 1,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Composite "what is this document related to?" view.

        Merges three graph-query backends into a single response so agents
        don't have to fan out across multiple tools and mentally join the
        results:

        - **links** — structural ``[[wiki-link]]`` navigation (NetworkX).
        - **provenance** — ``derived_from_ids`` chains (frontmatter index).
        - **edges** — typed LCMA edges (edges.db), both directions.

        For edge-table queries that are not centred on a single document
        (e.g. "list all ``contradicts`` edges", "audit a namespace"), use
        :func:`lithos_edge_list` instead — that tool is the only way to
        express filters like ``type`` alone or ``to_id`` alone.

        Args:
            id: Document UUID.
            include: Subset of ``["links", "provenance", "edges"]`` to
                populate. Defaults to all three. Unknown values are
                silently ignored so forward-compatible callers don't
                break when new backends land.
            depth: BFS depth 1-3 for ``links`` and ``provenance``.
                Ignored for ``edges`` (LCMA edges are a flat table).
            namespace: Optional namespace filter applied to ``edges``.
                ``links`` and ``provenance`` don't use namespaces.

        Returns:
            Dict shaped like::

                {
                  "id": "<doc-id>",
                  "included": ["links", "provenance", "edges"],
                  "links": {"outgoing": [...], "incoming": [...]},
                  "provenance": {
                      "sources": [...], "derived": [...],
                      "unresolved_sources": [...]
                  },
                  "edges": {"outgoing": [...], "incoming": [...]},
                  "related_ids": ["<id>", ...]   # deduped union
                }

            Sections not listed in ``include`` are omitted entirely
            (not emitted as empty keys). ``related_ids`` holds the
            deduped union of every id referenced across the included
            sections — sorted for determinism.
        """
        logger.info(
            "lithos_related: called",
            extra={"id": id, "include": include, "depth": depth, "namespace": namespace},
        )
        span = get_current_span()
        span.set_attribute("lithos.id", id)

        if not server.knowledge.has_document(id):
            return error_envelope("doc_not_found", f"Document not found: {id}")

        requested = include if include is not None else list(_RELATED_INCLUDES)
        selected = [k for k in _RELATED_INCLUDES if k in requested]
        span.set_attribute("lithos.include", ",".join(selected))

        depth = min(max(depth, 1), 3)
        span.set_attribute("lithos.depth", depth)

        result: dict[str, Any] = {
            "id": id,
            "included": selected,
        }
        related_ids: set[str] = set()

        # --- links (NetworkX wiki-links) ---------------------------
        if "links" in selected:
            links = server.graph.get_links(doc_id=id, direction="both", depth=depth)
            outgoing = [{"id": ln.id, "title": ln.title} for ln in links.outgoing]
            incoming = [{"id": ln.id, "title": ln.title} for ln in links.incoming]
            result["links"] = {"outgoing": outgoing, "incoming": incoming}
            related_ids.update(ln.id for ln in links.outgoing)
            related_ids.update(ln.id for ln in links.incoming)

        # --- provenance (frontmatter derived_from_ids) -------------
        if "provenance" in selected:
            sources = server.knowledge.provenance_neighbours(id, "sources", depth)
            derived = server.knowledge.provenance_neighbours(id, "derived", depth)
            unresolved_sources = sorted(server.knowledge.get_unresolved_sources(id))
            result["provenance"] = {
                "sources": sources,
                "derived": derived,
                "unresolved_sources": unresolved_sources,
            }
            related_ids.update(s["id"] for s in sources)
            related_ids.update(d["id"] for d in derived)

        # --- edges (LCMA edges.db) ---------------------------------
        if "edges" in selected:
            if server._config.lcma.enabled:
                # Fan out both directions; caller rarely wants just one.
                outgoing_edges = await server.projection.list_edges(from_id=id, namespace=namespace)
                incoming_edges = await server.projection.list_edges(to_id=id, namespace=namespace)
            else:
                outgoing_edges = []
                incoming_edges = []
            result["edges"] = {
                "outgoing": outgoing_edges,
                "incoming": incoming_edges,
            }
            for edge in outgoing_edges:
                tid = edge.get("to_id")
                if isinstance(tid, str):
                    related_ids.add(tid)
            for edge in incoming_edges:
                fid = edge.get("from_id")
                if isinstance(fid, str):
                    related_ids.add(fid)

        # Exclude the document itself from related_ids so callers
        # can iterate without filtering.
        related_ids.discard(id)
        result["related_ids"] = sorted(related_ids)
        span.set_attribute("lithos.related_count", len(related_ids))

        return result

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_tags(
        prefix: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Get all tags with document counts.

        Args:
            prefix: Optional prefix filter (case-insensitive). Only tags starting with this prefix are returned.

        Returns:
            Dict with tags mapping tag name to count
        """
        logger.info("lithos_tags prefix=%s", prefix)
        span = get_current_span()
        tags = await server.knowledge.get_all_tags()
        if prefix is not None:
            tags = {k: v for k, v in tags.items() if k.lower().startswith(prefix.lower())}
        span.set_attribute("lithos.tag_count", len(tags))
        return {"tags": tags}

    @mcp.tool()
    @tool_metrics()
    @tool_span()
    async def lithos_node_stats(
        node_id: str,
    ) -> dict[str, Any]:
        """View a note's salience score, retrieval stats, and penalty counts.

        Args:
            node_id: The document ID to look up stats for

        Returns:
            Dict with salience, retrieval_count, cited_count, ignored_count,
            misleading_count, and other stats fields.
            Returns error envelope if node_id does not match any known document.
        """
        logger.info("lithos_node_stats node_id=%s", node_id)
        span = get_current_span()
        span.set_attribute("lithos.node_id", node_id)

        result = await server.memory.node_stats(node_id)
        return result if isinstance(result, dict) else dataclasses.asdict(result)
