"""LCMA retrieval pipeline — orchestrates scouts, reranking, and receipts.

This module implements the ``lithos_retrieve`` pipeline:

1. **Phase A** — Fire scouts in parallel (vector, lexical, exact_alias,
   tags_recency, freshness, task_context) via ``asyncio.gather``.
2. **Phase B** — Fire provenance, graph, coactivation, and source_url scouts
   sequentially, seeded from top ``max_context_nodes`` of the Phase A
   normalised pool.
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
import re
import time
from typing import TYPE_CHECKING

from lithos.lcma.scouts import (
    ALL_SCOUT_NAMES,
    scout_coactivation,
    scout_contradictions,
    scout_exact_alias,
    scout_freshness,
    scout_graph,
    scout_lexical,
    scout_provenance,
    scout_source_url,
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
    from lithos.telemetry import _LithosMetrics

_lithos_metrics: _LithosMetrics | None = None
try:
    from lithos.telemetry import lithos_metrics as _lithos_metrics

    _HAS_TELEMETRY = True
except Exception:
    _HAS_TELEMETRY = False

logger = logging.getLogger(__name__)


_MMR_LAMBDA = 0.7  # 1.0 = relevance only, 0.0 = diversity only
_MMR_WINDOW = 30  # apply diversity over this many top candidates
_TOKEN_PATTERN = re.compile(r"\w+")


def _title_tokens(knowledge: KnowledgeManager, node_id: str) -> set[str]:
    """Lowercased token set for a node's title+path — used by MMR similarity."""
    cached = knowledge._meta_cache.get(node_id)
    if cached is None:
        return set()
    title = getattr(cached, "title", "") or ""
    path = str(getattr(cached, "path", "") or "")
    tokens = _TOKEN_PATTERN.findall(f"{title} {path}".lower())
    return set(tokens)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _mmr_diversify(
    ranked: list[Candidate],
    knowledge: KnowledgeManager,
    window: int = _MMR_WINDOW,
    lam: float = _MMR_LAMBDA,
) -> list[Candidate]:
    """Greedy MMR over the top ``window`` candidates by title-token Jaccard.

    Returns a reordered list: diversified top ``window`` followed by any tail
    left untouched. Input is not mutated.
    """
    if len(ranked) <= 1:
        return list(ranked)

    head = list(ranked[:window])
    tail = list(ranked[window:])
    token_cache: dict[str, set[str]] = {
        c.node_id: _title_tokens(knowledge, c.node_id) for c in head
    }

    selected: list[Candidate] = []
    remaining = list(head)
    while remaining:
        best_idx = 0
        best_score = -float("inf")
        for i, c in enumerate(remaining):
            if not selected:
                mmr = c.score
            else:
                max_sim = max(
                    _jaccard(token_cache[c.node_id], token_cache[s.node_id]) for s in selected
                )
                mmr = lam * c.score - (1.0 - lam) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_idx = i
        selected.append(remaining.pop(best_idx))

    return selected + tail


def _rerank_fast(
    candidates: list[Candidate],
    lcma_config: LcmaConfig,
    knowledge: KnowledgeManager,
    salience_map: dict[str, float] | None = None,
) -> list[Candidate]:
    """Terrace 1 reranking: weighted scout scores, note_type priors, salience.

    When *salience_map* is provided, actual salience values from StatsStore are
    used instead of the normalised scout score.  ``salience_map`` maps
    ``node_id → salience``; nodes absent from the map fall back to 0.5
    (the StatsStore default).

    After the linear combination sort, applies a greedy MMR pass over the top
    candidates to penalise near-duplicates (see checklist MVP 1 requirement for
    "diversity (MMR)").

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

        # Salience: read from StatsStore via pre-fetched map when available,
        # otherwise fall back to normalised score (pre-reinforcement path).
        salience = salience_map.get(c.node_id, 0.5) if salience_map is not None else c.score

        # Final composite: weighted combination
        final = c.score * scout_weight + note_type_prior * 0.1 + salience * 0.1
        scored.append(
            (
                final,
                Candidate(
                    node_id=c.node_id,
                    score=final,
                    reasons=list(c.reasons),
                    scouts=list(c.scouts),
                ),
            )
        )

    scored.sort(key=lambda t: t[0], reverse=True)
    ranked = [c for _, c in scored]
    return _mmr_diversify(ranked, knowledge)


def _dominant_namespace(
    node_ids: list[str],
    knowledge: KnowledgeManager,
) -> str:
    """Return the most common namespace among node_ids; ties broken alphabetically.

    Reads ``namespace`` from the metadata cache (which honors explicit
    frontmatter overrides) — never re-derived from path.
    """
    ns_counts: dict[str, int] = collections.Counter()
    for nid in node_ids:
        cached = knowledge._meta_cache.get(nid)
        if cached:
            ns_counts[cached.namespace] += 1
        else:
            ns_counts["default"] += 1
    if not ns_counts:
        return "default"
    max_count = max(ns_counts.values())
    # Among those with max count, pick alphabetically first
    return min(ns for ns, c in ns_counts.items() if c == max_count)


_COLD_START_TEMPERATURE = 0.5  # temperatures at or above this value indicate cold-start conditions
# NOTE: temperature_default is 0.5 (LcmaConfig default), which exactly meets this threshold,
# so the cold-start counter fires for every MVP1 call (no real graph data yet).
# In MVP3, compute_temperature will return values derived from edge coherence; only
# genuinely warm graphs (high coherence → low temperature) will stay below this threshold.


async def compute_temperature(
    edge_store: EdgeStore,
    lcma_config: LcmaConfig,
    namespace_filter: list[str] | None,
) -> float:
    """Return the MVP 1 retrieval temperature.

    MVP 1 always returns ``lcma_config.temperature_default`` — the design
    defers coherence-based computation (``temperature = 1 - coherence``) to
    MVP 3, when ``edges.db`` has been populated with enough typed edges to
    make coherence meaningful. The ``edge_store`` parameter is preserved in
    the signature so callers do not need to change when MVP 3 activates it.

    Temperature semantics: high temperature (≥ ``_COLD_START_TEMPERATURE``)
    indicates insufficient graph data (cold start). Low temperature indicates
    a well-connected graph with high coherence.
    """
    del edge_store, namespace_filter  # unused in MVP 1
    temperature = lcma_config.temperature_default
    if _HAS_TELEMETRY and _lithos_metrics is not None and temperature >= _COLD_START_TEMPERATURE:
        _lithos_metrics.lcma_temperature_cold_start.add(1)
    return temperature


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
    final_nodes: list[dict[str, object]] = []
    final_node_ids: list[str] = []
    candidates_considered = 0
    terrace_reached = 0
    temperature: float = lcma_config.temperature_default
    conflicts_found: list[dict[str, object]] = []
    _retrieve_t0 = time.perf_counter()

    try:
        # ── Phase A: parallel scouts ──────────────────────────────
        # All scouts receive the same set of caller-supplied filters
        # (namespace, scope, tags, path_prefix) so they enforce a
        # consistent global view, regardless of which backend they wrap.
        scout_kw = {
            "namespace_filter": namespace_filter,
            "agent_id": agent_id,
            "task_id": task_id,
            "tags": tags,
            "path_prefix": path_prefix,
        }

        # Phase A scout list — keep names parallel to coros so we can mark
        # each one as "executed" if it ran without raising. A scout that
        # ran and returned [] still counts as fired in the audit trail.
        from collections.abc import Awaitable

        phase_a_names: list[str] = [
            "scout_vector",
            "scout_lexical",
            "scout_exact_alias",
            "scout_tags_recency",
            "scout_freshness",
        ]
        phase_a_coros: list[Awaitable[list[Candidate]]] = [
            scout_vector(query, search, knowledge, limit=limit, **scout_kw),
            scout_lexical(query, search, knowledge, limit=limit, **scout_kw),
            scout_exact_alias(query, graph, knowledge, limit=limit, **scout_kw),
            scout_tags_recency(query, knowledge, limit=limit, **scout_kw),
            scout_freshness(query, knowledge, limit=limit, **scout_kw),
        ]
        # task_context only when task_id provided
        if task_id is not None:
            phase_a_names.append("scout_task_context")
            phase_a_coros.append(
                scout_task_context(coordination, knowledge, limit=limit, **scout_kw)
            )

        _phase_a_start = time.perf_counter()
        phase_a_results = await asyncio.gather(*phase_a_coros, return_exceptions=True)
        _phase_a_elapsed = time.perf_counter() - _phase_a_start

        # Collect successful results AND track which scouts ran cleanly.
        executed_scouts: set[str] = set()
        all_candidates: list[Candidate] = []
        for name, result in zip(phase_a_names, phase_a_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Phase A scout %s failed: %s", name, result)
                continue
            executed_scouts.add(name)
            all_candidates.extend(result)
            if _HAS_TELEMETRY and _lithos_metrics is not None:
                # Phase A scouts run concurrently via asyncio.gather, so all scouts
                # share the same wall-clock elapsed time (_phase_a_elapsed). This
                # records each scout's *share* of the Phase A wall-clock duration,
                # NOT its individual latency. Per-scout latencies are only meaningful
                # for Phase B scouts, which run sequentially.
                _lithos_metrics.lcma_scout_duration.record(_phase_a_elapsed * 1000, {"scout": name})
                _lithos_metrics.lcma_scout_candidates.record(len(result), {"scout": name})

        # ── Phase A normalisation for provenance seeding ──────────
        phase_a_normalised = merge_and_normalize(all_candidates)
        phase_a_normalised.sort(key=lambda c: c.score, reverse=True)

        # ── Phase B: sequential scouts seeded from Phase A ─────────
        seed_ids = [c.node_id for c in phase_a_normalised[:max_context_nodes]]
        if seed_ids:
            try:
                _t = time.perf_counter()
                prov_candidates = await scout_provenance(
                    seed_ids, knowledge, limit=limit, **scout_kw
                )
                executed_scouts.add("scout_provenance")
                all_candidates.extend(prov_candidates)
                if _HAS_TELEMETRY and _lithos_metrics is not None:
                    _lithos_metrics.lcma_scout_duration.record(
                        (time.perf_counter() - _t) * 1000, {"scout": "scout_provenance"}
                    )
                    _lithos_metrics.lcma_scout_candidates.record(
                        len(prov_candidates), {"scout": "scout_provenance"}
                    )
            except Exception:
                logger.warning("Phase B (provenance) failed", exc_info=True)

            try:
                _t = time.perf_counter()
                graph_candidates = await scout_graph(
                    seed_ids, graph, edge_store, knowledge, limit=limit, **scout_kw
                )
                executed_scouts.add("scout_graph")
                all_candidates.extend(graph_candidates)
                if _HAS_TELEMETRY and _lithos_metrics is not None:
                    _lithos_metrics.lcma_scout_duration.record(
                        (time.perf_counter() - _t) * 1000, {"scout": "scout_graph"}
                    )
                    _lithos_metrics.lcma_scout_candidates.record(
                        len(graph_candidates), {"scout": "scout_graph"}
                    )
            except Exception:
                logger.warning("Phase B (graph) failed", exc_info=True)

            try:
                _t = time.perf_counter()
                coact_candidates = await scout_coactivation(
                    seed_ids, stats_store, knowledge, limit=limit, **scout_kw
                )
                executed_scouts.add("scout_coactivation")
                all_candidates.extend(coact_candidates)
                if _HAS_TELEMETRY and _lithos_metrics is not None:
                    _lithos_metrics.lcma_scout_duration.record(
                        (time.perf_counter() - _t) * 1000, {"scout": "scout_coactivation"}
                    )
                    _lithos_metrics.lcma_scout_candidates.record(
                        len(coact_candidates), {"scout": "scout_coactivation"}
                    )
            except Exception:
                logger.warning("Phase B (coactivation) failed", exc_info=True)

            try:
                _t = time.perf_counter()
                src_url_candidates = await scout_source_url(
                    seed_ids, knowledge, limit=limit, **scout_kw
                )
                executed_scouts.add("scout_source_url")
                all_candidates.extend(src_url_candidates)
                if _HAS_TELEMETRY and _lithos_metrics is not None:
                    _lithos_metrics.lcma_scout_duration.record(
                        (time.perf_counter() - _t) * 1000, {"scout": "scout_source_url"}
                    )
                    _lithos_metrics.lcma_scout_candidates.record(
                        len(src_url_candidates), {"scout": "scout_source_url"}
                    )
            except Exception:
                logger.warning("Phase B (source_url) failed", exc_info=True)

        # Contradictions — only fire when surface_conflicts is True
        conflicts_found: list[dict[str, object]] = []
        if surface_conflicts:
            try:
                conflicts_found = await scout_contradictions(
                    seed_ids,
                    edge_store,
                    knowledge,
                    namespace_filter=namespace_filter,
                    agent_id=agent_id,
                    task_id=task_id,
                )
            except Exception:
                logger.warning("Phase B (contradictions) failed", exc_info=True)

        # Record scouts_fired using canonical names in order. A scout
        # appears here iff it executed without raising — empty result
        # sets still count as "fired" so the audit trail accurately
        # reflects what the pipeline did.
        scouts_fired = [s for s in ALL_SCOUT_NAMES if s in executed_scouts]

        # ── Merge & Normalise all candidates ──────────────────────
        merged = merge_and_normalize(all_candidates)
        candidates_considered = len(merged)

        # ── Terrace 1: rerank_fast ────────────────────────────────
        # ── Pre-fetch salience map for reranking ─────��────────────
        all_node_ids = [c.node_id for c in merged]
        stats_batch = await stats_store.get_node_stats_batch(all_node_ids)
        salience_map: dict[str, float] = {}
        for nid in all_node_ids:
            stats = stats_batch.get(nid)
            raw = stats["salience"] if stats else 0.5
            salience_map[nid] = raw if isinstance(raw, float) else 0.5

        reranked = _rerank_fast(merged, lcma_config, knowledge, salience_map=salience_map)
        terrace_reached = 1

        # Apply limit
        final_candidates = reranked[:limit]
        final_node_ids = [c.node_id for c in final_candidates]
        # Build receipt-shaped final_nodes: id + reasons + scouts so the
        # audit trail captures *why* each node was retrieved (design §4.6).
        final_nodes = [
            {
                "id": c.node_id,
                "reasons": list(c.reasons),
                "scouts": list(c.scouts),
            }
            for c in final_candidates
        ]

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

        envelope: dict[str, object] = {
            "results": results,
            "temperature": temperature,
            "terrace_reached": terrace_reached,
            "receipt_id": receipt_id,
        }
        if surface_conflicts:
            envelope["conflicts"] = conflicts_found
        return envelope

    finally:
        # ── OTEL retrieve metrics ─────────────────────────────────
        if _HAS_TELEMETRY and _lithos_metrics is not None:
            try:
                _retrieve_elapsed_ms = (time.perf_counter() - _retrieve_t0) * 1000
                _lithos_metrics.lcma_retrieve_duration.record(_retrieve_elapsed_ms)
                _lithos_metrics.lcma_retrieve_candidates_considered.record(candidates_considered)
                _lithos_metrics.lcma_retrieve_final_nodes.record(len(final_nodes))
            except Exception:
                logger.debug("run_retrieve: failed to record OTEL metrics", exc_info=True)

        # ── Receipt — always written (even on error) ──────────────
        try:
            await stats_store.insert_receipt(
                receipt_id=receipt_id,
                query=query,
                limit=limit,
                namespace_filter=namespace_filter,
                scouts_fired=scouts_fired,
                candidates_considered=candidates_considered,
                final_nodes=final_nodes,
                conflicts_surfaced=conflicts_found,
                surface_conflicts=surface_conflicts,
                temperature=temperature,
                terrace_reached=terrace_reached,
                agent_id=agent_id,
                task_id=task_id,
            )
        except Exception:
            logger.error("Failed to write receipt %s", receipt_id, exc_info=True)

        # ── Coactivation + node_stats (after receipt) ─────────────
        if final_node_ids:
            try:
                dom_ns = _dominant_namespace(final_node_ids, knowledge)

                # Batch-increment node_stats for all final nodes
                await stats_store.increment_node_stats_batch(final_node_ids)

                # Batch-increment coactivation for all unordered pairs
                pairs = list(itertools.combinations(final_node_ids, 2))
                await stats_store.increment_coactivation_batch(pairs, namespace=dom_ns)
            except Exception:
                logger.warning("Coactivation/node_stats update failed", exc_info=True)
