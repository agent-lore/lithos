"""LCMA retrieval pipeline — orchestrates scouts, reranking, and receipts.

This module implements the ``lithos_retrieve`` pipeline:

1. **Phase A** — Fire scouts in parallel (vector, lexical, exact_alias,
   tags_recency, freshness, task_context) via ``asyncio.gather``.
2. **Phase B** — Fire provenance scout sequentially, seeded from top
   ``max_context_nodes`` of the Phase A normalised pool.
3. **Merge & Normalise** — ``merge_and_normalize`` produces a unified pool.
4. **Terrace 1 Rerank** — Diversity (MMR), note-type priors, basic salience.
5. **Temperature** — Cold-start detection based on edge count.
6. **Receipt** — Write audit row to stats.db on every call (including errors).
7. **Working Memory** — Upsert rows when ``task_id`` is provided.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import logging
from typing import TYPE_CHECKING

from lithos.lcma.scouts import (
    ALL_SCOUT_NAMES,
    scout_contradictions,
    scout_exact_alias,
    scout_freshness,
    scout_lexical,
    scout_provenance,
    scout_tags_recency,
    scout_task_context,
    scout_vector,
)
from lithos.lcma.stats import StatsStore, _generate_receipt_id
from lithos.lcma.utils import Candidate, merge_and_normalize
from lithos.search import generate_snippet

if TYPE_CHECKING:
    from lithos.config import LcmaConfig
    from lithos.coordination import CoordinationService
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager
    from lithos.lcma.edges import EdgeStore
    from lithos.search import SearchEngine

logger = logging.getLogger(__name__)


def _rerank_fast(
    candidates: list[Candidate],
    lcma_config: LcmaConfig,
    knowledge: KnowledgeManager,
) -> list[Candidate]:
    """Terrace 1 reranking: weighted scout scores, note_type priors, basic salience.

    Returns a new sorted list (highest score first). Input is not mutated.
    """
    rerank_weights = lcma_config.rerank_weights
    note_type_priors = lcma_config.note_type_priors

    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        # Weighted scout contribution — average weight across all contributing scouts
        weight_sum = 0.0
        for scout_name in c.scouts:
            # Strip scout_ prefix to match rerank_weights keys
            key = scout_name.removeprefix("scout_")
            weight_sum += rerank_weights.get(key, 0.0)
        scout_weight = weight_sum / max(len(c.scouts), 1)

        # Note-type prior from metadata cache
        note_type_prior = 0.5
        cached = knowledge._meta_cache.get(c.node_id)
        if cached:
            note_type = getattr(cached, "note_type", None) or "observation"
            note_type_prior = note_type_priors.get(note_type, 0.5)

        # Salience: use normalized score as proxy (actual salience from stats
        # will be layered in US-011)
        salience = c.score

        # Final composite: weighted combination
        final = c.score * scout_weight + note_type_prior * 0.1 + salience * 0.1
        scored.append((final, c))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [c for _, c in scored]


def _dominant_namespace(
    node_ids: list[str],
    knowledge: KnowledgeManager,
) -> str:
    """Return the most common namespace among node_ids; ties broken alphabetically."""
    ns_counts: dict[str, int] = collections.Counter()
    for nid in node_ids:
        cached = knowledge._meta_cache.get(nid)
        if cached:
            from lithos.knowledge import derive_namespace

            ns = derive_namespace(cached.path)
            ns_counts[ns] += 1
        else:
            ns_counts["default"] += 1
    if not ns_counts:
        return "default"
    max_count = max(ns_counts.values())
    # Among those with max count, pick alphabetically first
    return min(ns for ns, c in ns_counts.items() if c == max_count)


async def compute_temperature(
    edge_store: EdgeStore,
    lcma_config: LcmaConfig,
    namespace_filter: list[str] | None,
) -> float:
    """Compute temperature — cold-start returns default when edge count is low."""
    try:
        # Use dominant namespace if filter provided, otherwise global count
        ns = namespace_filter[0] if namespace_filter else None
        edge_count = await edge_store.count(namespace=ns)
        if edge_count < lcma_config.temperature_edge_threshold:
            return lcma_config.temperature_default
        # When enough edges exist, temperature approaches 0 (confident retrieval)
        return max(0.0, 1.0 - edge_count / (lcma_config.temperature_edge_threshold * 10))
    except Exception:
        logger.warning("compute_temperature failed, using default", exc_info=True)
        return lcma_config.temperature_default


async def run_retrieve(
    *,
    query: str,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    graph: KnowledgeGraph,
    coordination: CoordinationService,
    edge_store: EdgeStore,
    stats_store: StatsStore,
    lcma_config: LcmaConfig,
    limit: int = 10,
    namespace_filter: list[str] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    surface_conflicts: bool = False,
    max_context_nodes: int | None = None,
    tags: list[str] | None = None,
    path_prefix: str | None = None,
) -> dict[str, object]:
    """Execute the full LCMA retrieval pipeline.

    Returns the response envelope with results, temperature, terrace_reached,
    and receipt_id.
    """
    if max_context_nodes is None:
        max_context_nodes = limit

    receipt_id = _generate_receipt_id()
    scouts_fired: list[str] = []
    final_nodes: list[str] = []
    terrace_reached = 0

    try:
        # ── Phase A: parallel scouts ──────────────────────────────
        common_kw = {
            "namespace_filter": namespace_filter,
            "agent_id": agent_id,
            "task_id": task_id,
        }
        search_kw = {**common_kw, "tags": tags, "path_prefix": path_prefix}

        phase_a_coros = [
            scout_vector(query, search, knowledge, limit=limit, **search_kw),
            scout_lexical(query, search, knowledge, limit=limit, **search_kw),
            scout_exact_alias(query, graph, knowledge, limit=limit, **common_kw),
            scout_tags_recency(query, knowledge, limit=limit, **search_kw),
            scout_freshness(query, knowledge, limit=limit, **common_kw),
        ]
        # task_context only when task_id provided
        if task_id is not None:
            phase_a_coros.append(
                scout_task_context(coordination, knowledge, limit=limit, **common_kw)
            )

        phase_a_results = await asyncio.gather(*phase_a_coros, return_exceptions=True)

        # Collect successful results
        all_candidates: list[Candidate] = []
        for result in phase_a_results:
            if isinstance(result, BaseException):
                logger.warning("Phase A scout failed: %s", result)
                continue
            all_candidates.extend(result)

        # Record which scouts fired
        fired_set: set[str] = set()
        for c in all_candidates:
            fired_set.update(c.scouts)

        # ── Phase A normalisation for provenance seeding ──────────
        phase_a_normalised = merge_and_normalize(all_candidates)
        phase_a_normalised.sort(key=lambda c: c.score, reverse=True)

        # ── Phase B: provenance (sequential, seeded from Phase A) ─
        seed_ids = [c.node_id for c in phase_a_normalised[:max_context_nodes]]
        if seed_ids:
            try:
                prov_candidates = await scout_provenance(
                    seed_ids, knowledge, limit=limit, **common_kw
                )
                all_candidates.extend(prov_candidates)
                for c in prov_candidates:
                    fired_set.update(c.scouts)
            except Exception:
                logger.warning("Phase B (provenance) failed", exc_info=True)

        # Contradictions stub (not counted)
        await scout_contradictions()

        # Record scouts_fired using canonical names in order
        scouts_fired = [s for s in ALL_SCOUT_NAMES if s in fired_set]

        # ── Merge & Normalise all candidates ──────────────────────
        merged = merge_and_normalize(all_candidates)

        # ── Terrace 1: rerank_fast ────────────────────────────────
        reranked = _rerank_fast(merged, lcma_config, knowledge)
        terrace_reached = 1

        # Apply limit
        final_candidates = reranked[:limit]
        final_nodes = [c.node_id for c in final_candidates]

        # ── Temperature ───────────────────────────────────────────
        temperature = await compute_temperature(edge_store, lcma_config, namespace_filter)

        # ── Build result dicts ────────────────────────────────────
        results: list[dict[str, object]] = []
        for c in final_candidates:
            try:
                doc, _ = await knowledge.read(id=c.node_id)
                meta = doc.metadata
                snippet = generate_snippet(doc.content, query)
                results.append(
                    {
                        "id": doc.id,
                        "title": doc.title,
                        "snippet": snippet,
                        "score": c.score,
                        "path": str(doc.path),
                        "source_url": meta.source_url or "",
                        "updated_at": meta.updated_at.isoformat() if meta.updated_at else "",
                        "is_stale": meta.is_stale,
                        "derived_from_ids": knowledge.get_doc_sources(doc.id),
                        # LCMA extras
                        "reasons": c.reasons,
                        "scouts": c.scouts,
                        "salience": c.score,
                    }
                )
            except FileNotFoundError:
                logger.warning("Document %s not found during result building", c.node_id)
                continue

        # ── Working memory upserts ────────────────────────────────
        if task_id is not None:
            for r in results:
                try:
                    await stats_store.upsert_working_memory(
                        task_id=task_id,
                        node_id=str(r["id"]),
                        receipt_id=receipt_id,
                    )
                except Exception:
                    logger.warning("Working memory upsert failed for %s", r["id"], exc_info=True)

        return {
            "results": results,
            "temperature": temperature,
            "terrace_reached": terrace_reached,
            "receipt_id": receipt_id,
        }

    finally:
        # ── Receipt — always written (even on error) ──────────────
        try:
            await stats_store.insert_receipt(
                receipt_id=receipt_id,
                query=query,
                limit=limit,
                namespace_filter=namespace_filter,
                scouts_fired=scouts_fired,
                final_nodes=final_nodes,
                conflicts_surfaced=[],
                temperature=0.0 if not terrace_reached else temperature,  # type: ignore[possibly-undefined]
                terrace_reached=terrace_reached,
                agent_id=agent_id,
                task_id=task_id,
            )
        except Exception:
            logger.error("Failed to write receipt %s", receipt_id, exc_info=True)

        # ── Coactivation + node_stats (after receipt) ─────────────
        if final_nodes:
            try:
                dom_ns = _dominant_namespace(final_nodes, knowledge)

                # Increment node_stats for every node in final_nodes
                for nid in final_nodes:
                    await stats_store.increment_node_stats(node_id=nid)

                # Increment coactivation for every unordered pair
                for a, b in itertools.combinations(final_nodes, 2):
                    await stats_store.increment_coactivation(node_a=a, node_b=b, namespace=dom_ns)
            except Exception:
                logger.warning("Coactivation/node_stats update failed", exc_info=True)
