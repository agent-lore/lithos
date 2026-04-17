"""LCMA Scout functions — seven MVP 1 scouts.

Each scout wraps existing Lithos infrastructure and returns a list of
``Candidate`` objects with raw (pre-normalisation) scores.  Normalisation
is handled by ``merge_and_normalize`` in the retrieval pipeline.

All scouts are async-safe: synchronous backends (Tantivy, ChromaDB) are
wrapped with ``asyncio.to_thread()``.

Every scout applies **namespace_filter** gating and **access_scope** gating
before returning candidates.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from lithos.lcma.utils import Candidate

if TYPE_CHECKING:
    from lithos.coordination import CoordinationService
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager
    from lithos.lcma.edges import EdgeStore
    from lithos.lcma.stats import StatsStore
    from lithos.search import SearchEngine

logger = logging.getLogger(__name__)

# Canonical scout names — used in receipts.scouts_fired
SCOUT_VECTOR = "scout_vector"
SCOUT_LEXICAL = "scout_lexical"
SCOUT_EXACT_ALIAS = "scout_exact_alias"
SCOUT_TAGS_RECENCY = "scout_tags_recency"
SCOUT_FRESHNESS = "scout_freshness"
SCOUT_PROVENANCE = "scout_provenance"
SCOUT_TASK_CONTEXT = "scout_task_context"
SCOUT_GRAPH = "scout_graph"
SCOUT_COACTIVATION = "scout_coactivation"
SCOUT_SOURCE_URL = "scout_source_url"

ALL_SCOUT_NAMES = [
    SCOUT_VECTOR,
    SCOUT_LEXICAL,
    SCOUT_EXACT_ALIAS,
    SCOUT_TAGS_RECENCY,
    SCOUT_FRESHNESS,
    SCOUT_PROVENANCE,
    SCOUT_TASK_CONTEXT,
    SCOUT_GRAPH,
    SCOUT_COACTIVATION,
    SCOUT_SOURCE_URL,
]

# Keywords that trigger freshness boost
_FRESHNESS_KEYWORDS = re.compile(r"\b(update|refresh|recheck|verify|latest)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Gating helpers
# ---------------------------------------------------------------------------


def _passes_namespace_filter(namespace: str | None, namespace_filter: list[str] | None) -> bool:
    """Return True if namespace passes the filter (or no filter is set)."""
    if namespace_filter is None:
        return True
    if namespace is None:
        return False
    return namespace in namespace_filter


def _passes_access_scope(
    access_scope: str | None,
    author: str | None,
    source: str | None,
    agent_id: str | None,
    task_id: str | None,
) -> bool:
    """Return True if the note's access_scope allows this caller to see it.

    - ``shared``: always visible
    - ``task``: visible only if caller's task_id matches note's source
    - ``agent_private``: visible only if caller's agent_id matches note's author
    """
    if access_scope is None or access_scope == "shared":
        return True
    if access_scope == "task":
        if not task_id or not source:
            return False
        return source == task_id
    if access_scope == "agent_private":
        if not agent_id or not author:
            return False
        return author == agent_id
    return True


def _passes_tags_filter(meta_tags: list[str] | None, tags: list[str] | None) -> bool:
    """Return True if the note has at least one of the requested tags.

    ``tags=None`` (the common case) means "no filter" — all notes pass.
    Empty list also means "no filter".
    """
    if not tags:
        return True
    if not meta_tags:
        return False
    return any(t in meta_tags for t in tags)


def _passes_path_prefix(meta_path: object | None, path_prefix: str | None) -> bool:
    """Return True if the note's path starts with ``path_prefix``.

    ``path_prefix=None`` or empty string means "no filter" — all notes pass.
    """
    if not path_prefix:
        return True
    if meta_path is None:
        return False
    return str(meta_path).startswith(path_prefix)


# ---------------------------------------------------------------------------
# Scout implementations
# ---------------------------------------------------------------------------


async def scout_vector(
    query: str,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """ChromaDB semantic search via asyncio.to_thread."""
    results = await asyncio.to_thread(
        search.semantic_search,
        query=query,
        limit=limit * 3,  # over-fetch to allow for gating
        tags=tags,
        path_prefix=path_prefix,
    )
    candidates: list[Candidate] = []
    for r in results:
        doc_id = r.id
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=r.similarity,
                reasons=[f"semantic similarity {r.similarity:.3f}"],
                scouts=[SCOUT_VECTOR],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_vector: completed",
        extra={"fetched": len(results), "candidates": len(candidates), "limit": limit},
    )
    return candidates


async def scout_lexical(
    query: str,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Tantivy full-text search via asyncio.to_thread."""
    results = await asyncio.to_thread(
        search.full_text_search,
        query=query,
        limit=limit * 3,
        tags=tags,
        path_prefix=path_prefix,
    )
    candidates: list[Candidate] = []
    for r in results:
        doc_id = r.id
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=r.score,
                reasons=[f"lexical match score {r.score:.3f}"],
                scouts=[SCOUT_LEXICAL],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_lexical: completed",
        extra={"fetched": len(results), "candidates": len(candidates), "limit": limit},
    )
    return candidates


async def scout_exact_alias(
    query: str,
    graph: KnowledgeGraph,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Resolve query via wiki-link resolution, UUID-prefix, and slug matching."""
    found_ids: list[str] = []

    # 1. KnowledgeGraph._resolve_link (node_id == doc_id in graph)
    resolved = graph._resolve_link(query)
    if resolved and knowledge.has_document(resolved):
        found_ids.append(resolved)

    # 2. UUID-prefix matching
    query_lower = query.lower().strip()
    for doc_id in knowledge._id_to_path:
        if doc_id.lower().startswith(query_lower) and doc_id not in found_ids:
            found_ids.append(doc_id)

    # 3. Slug matching via KnowledgeManager.get_id_by_slug
    slug_id = knowledge.get_id_by_slug(query)
    if slug_id and slug_id not in found_ids:
        found_ids.append(slug_id)

    # Apply gating and build candidates
    candidates: list[Candidate] = []
    for doc_id in found_ids:
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=1.0,
                reasons=["exact alias/link match"],
                scouts=[SCOUT_EXACT_ALIAS],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_exact_alias: completed",
        extra={"resolved": len(found_ids), "candidates": len(candidates)},
    )
    return candidates


async def scout_tags_recency(
    query: str,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Tag + path_prefix filter sorted by recency. Returns [] when both are absent."""
    if not tags and not path_prefix:
        return []

    docs, _ = await knowledge.list_all(
        tags=tags,
        path_prefix=path_prefix,
        limit=limit * 3,
        exclude_status=["quarantined"],
    )
    # Sort by updated_at descending (most recent first)
    docs.sort(key=lambda d: d.metadata.updated_at, reverse=True)

    candidates: list[Candidate] = []
    for i, doc in enumerate(docs):
        meta = doc.metadata
        ns = meta.namespace
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(meta.access_scope, meta.author, meta.source, agent_id, task_id):
            continue
        # Recency score: higher for more recent, decaying with rank
        recency_score = 1.0 / (1.0 + i)
        candidates.append(
            Candidate(
                node_id=meta.id,
                score=recency_score,
                reasons=[f"tag/path match, recency rank {i + 1}"],
                scouts=[SCOUT_TAGS_RECENCY],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_tags_recency: completed",
        extra={"candidates": len(candidates), "tags": tags, "path_prefix": path_prefix},
    )
    return candidates


async def scout_freshness(
    query: str,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Boost notes with expires_at/is_stale, keyword-triggered."""
    if not _FRESHNESS_KEYWORDS.search(query):
        return []

    # Push tag/path filters down to list_all so the candidate set is narrower.
    docs, _ = await knowledge.list_all(
        tags=tags,
        path_prefix=path_prefix,
        limit=limit * 5,
        exclude_status=["quarantined"],
    )
    candidates: list[Candidate] = []
    for doc in docs:
        meta = doc.metadata
        if meta.expires_at is None:
            continue
        ns = meta.namespace
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(meta.access_scope, meta.author, meta.source, agent_id, task_id):
            continue
        # Stale notes get higher score (they need refreshing)
        score = 1.0 if meta.is_stale else 0.5
        candidates.append(
            Candidate(
                node_id=meta.id,
                score=score,
                reasons=["stale/expiring note" if meta.is_stale else "has expiry date"],
                scouts=[SCOUT_FRESHNESS],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_freshness: completed",
        extra={"candidates": len(candidates)},
    )
    return candidates


async def scout_provenance(
    seed_ids: list[str],
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Forward/reverse walk of derived_from_ids provenance index."""
    seen: set[str] = set(seed_ids)
    related: list[tuple[str, str]] = []  # (doc_id, reason)

    for seed_id in seed_ids:
        # Forward: what does this doc derive from?
        sources = knowledge.get_doc_sources(seed_id)
        for src_id in sources:
            if src_id not in seen:
                seen.add(src_id)
                related.append((src_id, f"source of {seed_id[:8]}"))

        # Reverse: what derives from this doc?
        derived = knowledge.get_derived_docs(seed_id)
        for der_id in derived:
            if der_id not in seen:
                seen.add(der_id)
                related.append((der_id, f"derived from {seed_id[:8]}"))

    candidates: list[Candidate] = []
    for doc_id, reason in related:
        if not knowledge.has_document(doc_id):
            continue
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        # All provenance hits get equal raw score
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=1.0,
                reasons=[reason],
                scouts=[SCOUT_PROVENANCE],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_provenance: completed",
        extra={"seed_count": len(seed_ids), "related": len(related), "candidates": len(candidates)},
    )
    return candidates


async def scout_task_context(
    coordination: CoordinationService,
    knowledge: KnowledgeManager,
    *,
    task_id: str | None = None,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Pull notes linked to this task via findings or by authorship.

    Returns ``[]`` when ``task_id`` is None. Candidates come from two sources:

    1. Findings for this task that carry a ``knowledge_id`` (from
       ``CoordinationService.list_findings``).
    2. Notes whose frontmatter ``source`` field matches ``task_id`` (i.e. the
       note was authored *for* this task). This replaces a prior over-broad
       sweep over every note authored by any agent with a non-expired claim,
       which could flood results with unrelated notes from long-lived agents.
    """
    if task_id is None:
        return []

    found_ids: set[str] = set()

    # 1. Findings with non-null knowledge_id
    findings = await coordination.list_findings(task_id)
    for f in findings:
        if f.knowledge_id is not None:
            found_ids.add(f.knowledge_id)

    # 2. Notes whose frontmatter source field points at this task_id
    for doc_id, cached in knowledge.iter_cached_meta():
        if getattr(cached, "source", None) == task_id:
            found_ids.add(doc_id)

    candidates: list[Candidate] = []
    for doc_id in found_ids:
        if not knowledge.has_document(doc_id):
            continue
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=1.0,
                reasons=["task context match"],
                scouts=[SCOUT_TASK_CONTEXT],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_task_context: completed",
        extra={"task_id": task_id, "candidates": len(candidates)},
    )
    return candidates


async def scout_graph(
    seed_ids: list[str],
    graph: KnowledgeGraph,
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Find neighbors of seed nodes in the wiki-link graph and typed edge graph."""
    seed_set = set(seed_ids)
    # Collect (neighbor_id, score, reason) — higher score wins on dedup
    neighbor_best: dict[str, tuple[float, str]] = {}

    def _track(nid: str, score: float, reason: str) -> None:
        prev = neighbor_best.get(nid)
        if prev is None or score > prev[0]:
            neighbor_best[nid] = (score, reason)

    # 1. Wiki-link graph (NetworkX)
    for seed_id in seed_ids:
        link_info = graph.get_links(seed_id, direction="both")
        for linked in link_info.outgoing:
            if linked.id not in seed_set:
                _track(linked.id, 0.5, f"wiki-link from {seed_id[:8]}")
        for linked in link_info.incoming:
            if linked.id not in seed_set:
                _track(linked.id, 0.5, f"wiki-link to {seed_id[:8]}")

    # 2. Typed edge graph (edges.db)
    for seed_id in seed_ids:
        outgoing = await edge_store.list_edges(from_id=seed_id)
        for edge in outgoing:
            nid = str(edge["to_id"])
            if nid not in seed_set:
                raw_w = edge["weight"]
                w = float(raw_w) if isinstance(raw_w, (int, float)) else 1.0
                _track(nid, w, f"{edge['type']} edge from {seed_id[:8]}")
        incoming = await edge_store.list_edges(to_id=seed_id)
        for edge in incoming:
            nid = str(edge["from_id"])
            if nid not in seed_set:
                raw_w = edge["weight"]
                w = float(raw_w) if isinstance(raw_w, (int, float)) else 1.0
                _track(nid, w, f"{edge['type']} edge to {seed_id[:8]}")

    # Sort by score descending, then apply gating
    ranked = sorted(neighbor_best.items(), key=lambda t: t[1][0], reverse=True)
    candidates: list[Candidate] = []
    for nid, (score, reason) in ranked:
        if not knowledge.has_document(nid):
            continue
        meta = _get_cached_meta(knowledge, nid)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=nid,
                score=score,
                reasons=[reason],
                scouts=[SCOUT_GRAPH],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_graph: completed",
        extra={
            "seed_count": len(seed_ids),
            "neighbors_found": len(neighbor_best),
            "candidates": len(candidates),
        },
    )
    return candidates


async def scout_coactivation(
    seed_ids: list[str],
    stats_store: StatsStore,
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Find nodes frequently co-occurring with seed nodes in past retrievals."""
    if not seed_ids:
        return []

    # When namespace_filter is set, scope coactivation queries per-namespace
    # to avoid leaking cross-namespace coactivation evidence.
    if namespace_filter:
        merged: dict[str, int] = {}
        for ns in namespace_filter:
            for node_id, count in await stats_store.get_coactivated(
                seed_ids, namespace=ns, limit=limit * 3
            ):
                merged[node_id] = merged.get(node_id, 0) + count
        coactivated = sorted(merged.items(), key=lambda t: t[1], reverse=True)[: limit * 3]
    else:
        coactivated = await stats_store.get_coactivated(seed_ids, limit=limit * 3)

    candidates: list[Candidate] = []
    for node_id, count in coactivated:
        if not knowledge.has_document(node_id):
            continue
        meta = _get_cached_meta(knowledge, node_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=node_id,
                score=float(count),
                reasons=[f"coactivated {count} times with seeds"],
                scouts=[SCOUT_COACTIVATION],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_coactivation: completed",
        extra={
            "seed_count": len(seed_ids),
            "coactivated": len(coactivated),
            "candidates": len(candidates),
        },
    )
    return candidates


def _extract_domain(normalized_url: str) -> str | None:
    """Extract the hostname from a normalized URL."""
    try:
        parsed = urlparse(normalized_url)
        return parsed.hostname or None
    except Exception:
        return None


async def scout_source_url(
    seed_ids: list[str],
    knowledge: KnowledgeManager,
    *,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> list[Candidate]:
    """Find notes from the same URL domain as seed notes."""
    seed_set = set(seed_ids)
    url_map = knowledge._source_url_to_id

    # 1. Collect domains from seed notes using the in-memory metadata cache
    #    (avoids synchronous disk I/O from frontmatter parsing). This handles
    #    both URL owners and non-owners in the _source_url_to_id map.
    seed_domains: set[str] = set()
    for seed_id in seed_ids:
        cached = knowledge.get_cached_meta(seed_id)
        if cached and cached.source_url:
            domain = _extract_domain(cached.source_url)
            if domain:
                seed_domains.add(domain)

    if not seed_domains:
        return []

    # 2. Find all notes with matching domains (excluding seeds)
    matches: list[str] = []
    for norm_url, doc_id in url_map.items():
        if doc_id in seed_set:
            continue
        domain = _extract_domain(norm_url)
        if domain and domain in seed_domains:
            matches.append(doc_id)

    # 3. Apply gating and build candidates
    candidates: list[Candidate] = []
    for doc_id in matches:
        if not knowledge.has_document(doc_id):
            continue
        meta = _get_cached_meta(knowledge, doc_id)
        if not _passes_status_filter(meta, ["quarantined"]):
            continue
        ns = meta.namespace if meta else None
        if not _passes_namespace_filter(ns, namespace_filter):
            continue
        if not _passes_access_scope(
            meta.access_scope if meta else None,
            meta.author if meta else None,
            meta.source if meta else None,
            agent_id,
            task_id,
        ):
            continue
        if not _passes_tags_filter(meta.tags if meta else None, tags):
            continue
        if not _passes_path_prefix(meta.path if meta else None, path_prefix):
            continue
        candidates.append(
            Candidate(
                node_id=doc_id,
                score=1.0,
                reasons=["same source URL domain"],
                scouts=[SCOUT_SOURCE_URL],
            )
        )
        if len(candidates) >= limit:
            break
    logger.debug(
        "scout_source_url: completed",
        extra={
            "seed_count": len(seed_ids),
            "domain_matches": len(matches),
            "candidates": len(candidates),
        },
    )
    return candidates


async def scout_contradictions(
    seed_ids: list[str],
    edge_store: EdgeStore,
    knowledge: KnowledgeManager,
    *,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, object]]:
    """Query contradiction edges connected to seed nodes.

    Returns a list of dicts with edge_id, from_id, to_id, and conflict_state
    for edges where conflict_state is NULL or not in a resolved terminal state.
    """
    _RESOLVED_STATES = {"superseded", "refuted", "merged"}
    seen_edge_ids: set[str] = set()
    results: list[dict[str, object]] = []

    for node_id in seed_ids:
        outgoing = await edge_store.list_edges(from_id=node_id, edge_type="contradicts")
        incoming = await edge_store.list_edges(to_id=node_id, edge_type="contradicts")

        for edge in [*outgoing, *incoming]:
            eid = str(edge["edge_id"])
            if eid in seen_edge_ids:
                continue
            seen_edge_ids.add(eid)

            conflict_state = edge["conflict_state"]
            if isinstance(conflict_state, str) and conflict_state in _RESOLVED_STATES:
                continue

            # Determine the counterpart note (the one that isn't the current seed)
            from_id = str(edge["from_id"])
            to_id = str(edge["to_id"])
            counterpart_id = to_id if from_id == node_id else from_id

            # Verify counterpart exists
            if not knowledge.has_document(counterpart_id):
                continue

            # Apply gating filters on the counterpart
            meta = _get_cached_meta(knowledge, counterpart_id)
            if not _passes_status_filter(meta, ["quarantined"]):
                continue
            ns = meta.namespace if meta else None
            if not _passes_namespace_filter(ns, namespace_filter):
                continue
            if not _passes_access_scope(
                meta.access_scope if meta else None,
                meta.author if meta else None,
                meta.source if meta else None,
                agent_id,
                task_id,
            ):
                continue

            results.append(
                {
                    "edge_id": eid,
                    "from_id": from_id,
                    "to_id": to_id,
                    "conflict_state": conflict_state,
                }
            )

    logger.debug(
        "scout_contradictions: completed",
        extra={"seed_count": len(seed_ids), "unresolved_conflicts": len(results)},
    )
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _CachedMetaView:
    """Typed view of cached metadata for gating decisions."""

    namespace: str
    access_scope: str
    author: str
    source: str | None
    tags: list[str]
    path: object  # Path, but kept loose to avoid an import cycle
    status: str | None = None


def _passes_status_filter(
    view: _CachedMetaView | None,
    exclude_status: list[str] | None = None,
) -> bool:
    """Return False when the view's status is in the exclusion list."""
    if exclude_status is None or view is None:
        return True
    return view.status not in exclude_status


def _get_cached_meta(knowledge: KnowledgeManager, doc_id: str) -> _CachedMetaView | None:
    """Read lightweight metadata from KnowledgeManager's in-memory cache.

    The namespace comes directly from the cache (which honors explicit
    frontmatter overrides) — never re-derived from path here.
    """
    cached = knowledge.get_cached_meta(doc_id)
    if cached is None:
        return None
    return _CachedMetaView(
        namespace=cached.namespace,
        access_scope=cached.access_scope or "shared",
        author=cached.author,
        source=cached.source,
        tags=list(cached.tags),
        path=cached.path,
        status=cached.status,
    )
