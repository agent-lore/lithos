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
import dataclasses
import itertools
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lithos.errors import ScoutFailure
from lithos.lcma.salience import DEFAULT_SALIENCE, usage_score
from lithos.lcma.scouts import (
    ALL_SCOUT_NAMES,
    SCOUT_CONTRADICTIONS,
    SCOUT_REGISTRY,
    ScoutContext,
    scout_contradictions,
)
from lithos.lcma.stats import StatsStore, _generate_receipt_id
from lithos.lcma.utils import Candidate, merge_and_normalize
from lithos.search import generate_snippet

if TYPE_CHECKING:
    from lithos.config import LcmaConfig
    from lithos.coordination import CoordinationService
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager

    # ``EdgeStore`` is used here as a type annotation only — for the
    # ``compute_temperature`` parameter, which the MVP1 stub does not
    # actually read. We import via ``lithos.provenance`` (the legitimate
    # gatekeeper per ADR-0004 / issue #251) so the rule "no edges-module
    # import outside provenance.py" holds. ``compute_temperature``
    # migrates to projection-owned APIs alongside the LCMA scaffolding
    # work in #255.
    from lithos.provenance import EdgeStore, ProvenanceProjection
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
    cached = knowledge.get_cached_meta(node_id)
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


def _days_since(ts: object, now: datetime) -> float | None:
    """Fractional days between an ISO timestamp and *now* (tz-naive treated as UTC).

    Returns ``None`` when *ts* is missing/blank/unparseable so a single malformed
    timestamp drops the recency term rather than failing the whole retrieval.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # Normalise a trailing 'Z' to +00:00 (as server.py / coordination.py do) so
        # older/alternate stored formats parse rather than silently dropping recency.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _usage_from_stats(
    stats: dict[str, object] | None,
    now: datetime,
    lcma_config: LcmaConfig,
) -> float:
    """Compute the non-decaying usage signal for a node from its stats row.

    Reads ``retrieval_count`` and last-use recency (``last_used_at`` falling back to
    ``last_retrieved_at``) — both already present in the pre-fetched row — and defers
    to :func:`lithos.lcma.salience.usage_score`. Unseen nodes score 0.0.
    """
    if stats is None:
        return 0.0
    raw_count = stats.get("retrieval_count")
    retrieval_count = raw_count if isinstance(raw_count, int) else 0
    days_since_use = _days_since(stats.get("last_used_at") or stats.get("last_retrieved_at"), now)
    return usage_score(
        retrieval_count,
        days_since_use,
        freq_weight=lcma_config.usage_freq_weight,
        recency_weight=lcma_config.usage_recency_weight,
        recency_halflife_days=lcma_config.usage_recency_halflife_days,
        freq_norm_k=lcma_config.usage_freq_norm_k,
    )


def _rerank_fast(
    candidates: list[Candidate],
    lcma_config: LcmaConfig,
    knowledge: KnowledgeManager,
    salience_map: dict[str, float] | None = None,
    usage_map: dict[str, float] | None = None,
) -> list[Candidate]:
    """Terrace 1 reranking: weighted scout scores, note_type priors, salience, usage.

    When *salience_map* is provided, actual salience values from StatsStore are
    used instead of the normalised scout score.  ``salience_map`` maps
    ``node_id → salience``; nodes absent from the map fall back to
    ``DEFAULT_SALIENCE`` (the StatsStore default).

    *usage_map* carries the non-decaying popularity signal (retrieval frequency +
    recency) per node; nodes absent from it — or the whole map being ``None`` — score
    0.0 (neutral). Unlike salience this cannot collapse, so it restores a real spread
    to the composite even on a cold, unreinforced corpus (task ``e7d8ef60``).

    The additive term weights (salience, note_type, usage) come from
    :class:`LcmaConfig` rather than hardcoded coefficients.

    After the linear combination sort, applies a greedy MMR pass over the top
    candidates to penalise near-duplicates (see checklist MVP 1 requirement for
    "diversity (MMR)").

    Returns a new sorted list (highest score first). Input is not mutated.
    """
    rerank_weights = lcma_config.rerank_weights
    note_type_priors = lcma_config.note_type_priors
    w_salience = lcma_config.rerank_salience_weight
    w_note_type = lcma_config.rerank_note_type_weight
    w_usage = lcma_config.rerank_usage_weight

    debug_rows: list[dict[str, object]] = []
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
        resolved_note_type = "unknown"  # for calibration logging only
        cached = knowledge.get_cached_meta(c.node_id)
        if cached:
            note_type = getattr(cached, "note_type", None) or "observation"
            note_type_prior = note_type_priors.get(note_type, 0.5)
            resolved_note_type = note_type

        # Salience: read from StatsStore via pre-fetched map when available,
        # otherwise fall back to normalised score (pre-reinforcement path).
        salience = (
            salience_map.get(c.node_id, DEFAULT_SALIENCE) if salience_map is not None else c.score
        )

        # Usage: non-decaying popularity signal; neutral (0.0) when absent.
        usage = usage_map.get(c.node_id, 0.0) if usage_map is not None else 0.0

        # Final composite: weighted combination
        final = (
            c.score * scout_weight
            + note_type_prior * w_note_type
            + salience * w_salience
            + usage * w_usage
        )
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
        # Capture per-candidate score breakdown for calibration debugging (#179).
        # Only collected when DEBUG is enabled to avoid overhead on the hot path.
        if logger.isEnabledFor(logging.DEBUG):
            debug_rows.append(
                {
                    "node_id": c.node_id,
                    "scouts": list(c.scouts),
                    "base_score": round(c.score, 4),
                    "scout_weight": round(scout_weight, 4),
                    "note_type": resolved_note_type,
                    "note_type_prior": round(note_type_prior, 4),
                    "salience": round(salience, 4),
                    "usage_score": round(usage, 4),
                    "final": round(final, 4),
                }
            )

    scored.sort(key=lambda t: t[0], reverse=True)
    ranked = [c for _, c in scored]

    # Emit calibration breakdown at DEBUG (per-candidate) and a top-N summary
    # at INFO so over-ranking regressions like #179 are observable in prod
    # without requiring DEBUG.
    if debug_rows:
        debug_rows.sort(key=lambda r: r["final"], reverse=True)  # type: ignore[arg-type,return-value]
        logger.debug(
            "_rerank_fast: per-candidate score breakdown",
            extra={"candidates": debug_rows[:50]},
        )
    if ranked:
        top_summary = [
            {
                "node_id": c.node_id,
                "scouts": list(c.scouts),
                "final": round(c.score, 4),
            }
            for c in ranked[:10]
        ]
        logger.info(
            "_rerank_fast: ranked top-N",
            extra={
                "num_candidates": len(ranked),
                "top": top_summary,
            },
        )

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
        cached = knowledge.get_cached_meta(nid)
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


def _log_scout_failure(name: str, cause: BaseException, *, failed_scouts: set[str]) -> None:
    """Record a per-scout backend failure (ADR-0005: one bad scout can't kill retrieve).

    Adds *name* to *failed_scouts* (surfaced as the retrieve envelope's
    ``degraded``/``failed_scouts`` fields), bumps the ``lcma_scout_failures``
    counter, and logs with :class:`ScoutFailure` context (raised-and-caught so the
    log line carries that context).
    """
    failed_scouts.add(name)
    if _HAS_TELEMETRY and _lithos_metrics is not None:
        _lithos_metrics.lcma_scout_failures.add(1, {"scout": name})
    try:
        raise ScoutFailure(scout=name, cause=cause) from cause
    except ScoutFailure as exc:
        logger.warning(
            "run_retrieve: scout failed",
            extra={"scout": name, "error": str(exc.cause)},
            exc_info=True,
        )


def _record_scout_success(
    name: str,
    result: list[Candidate],
    duration_ms: float,
    *,
    all_candidates: list[Candidate],
    executed_scouts: set[str],
) -> None:
    """Fold one scout's candidates into the pool and mark it fired.

    ``duration_ms`` is the scout's Phase B latency, or its share of the Phase A
    wall-clock (all Phase A scouts share one gather duration). A scout that ran and
    returned ``[]`` still counts as fired in the audit trail.
    """
    executed_scouts.add(name)
    all_candidates.extend(result)
    if _HAS_TELEMETRY and _lithos_metrics is not None:
        _lithos_metrics.lcma_scout_duration.record(duration_ms, {"scout": name})
        _lithos_metrics.lcma_scout_candidates.record(len(result), {"scout": name})
    logger.debug(
        "run_retrieve: scout completed",
        extra={"scout": name, "candidates": len(result)},
    )


async def _run_retrieve_impl(
    *,
    query: str,
    search: SearchEngine,
    knowledge: KnowledgeManager,
    graph: KnowledgeGraph,
    coordination: CoordinationService,
    edge_store: EdgeStore,
    projection: ProvenanceProjection,
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
    """Execute the full LCMA retrieval pipeline (module-private).

    The public seam is :meth:`lithos.cognitive_memory.CognitiveMemory.retrieve`,
    which wires the six store dependencies from its own state and forwards the
    request here. Direct callers (tests, future internal pipelines) may import
    this implementation function under its private name.

    Returns the response envelope with results, temperature, terrace_reached,
    receipt_id, and the degraded-mode signal ``degraded`` (bool) / ``failed_scouts``
    (canonical names of scouts that ran and raised).

    Errors:
        - Per-scout backend failures are wrapped in :class:`ScoutFailure` and
          caught at one documented boundary inside this function (so a single
          misbehaving backend cannot kill the whole retrieve). The audit trail
          (``scouts_fired``) reflects which scouts ran cleanly; ``failed_scouts``
          reflects which ran and raised.
        - All other exceptions (e.g. ``StatsStore`` I/O failures) propagate
          to the caller; they are not wrapped in ``ScoutFailure``.
    """
    if max_context_nodes is None:
        max_context_nodes = limit

    receipt_id = _generate_receipt_id()
    scouts_fired: list[str] = []
    failed_scout_names: set[str] = set()
    final_nodes: list[dict[str, object]] = []
    final_node_ids: list[str] = []
    candidates_considered = 0
    terrace_reached = 0
    temperature: float = lcma_config.temperature_default
    conflicts_found: list[dict[str, object]] = []
    _retrieve_t0 = time.perf_counter()

    logger.info(
        "run_retrieve: started",
        extra={
            "receipt_id": receipt_id,
            "query_len": len(query),
            "limit": limit,
            "agent_id": agent_id,
            "task_id": task_id,
            "namespace_filter": namespace_filter,
            "max_context_nodes": max_context_nodes,
        },
    )

    try:
        # ── Phase A: parallel scouts ──────────────────────────────
        # Every scout receives the same caller-supplied filters (namespace, scope,
        # tags, path_prefix) via the shared ScoutContext, so they enforce a
        # consistent global view regardless of which backend they wrap.
        base_ctx = ScoutContext(
            query=query,
            seed_ids=[],
            search=search,
            knowledge=knowledge,
            graph=graph,
            projection=projection,
            stats_store=stats_store,
            coordination=coordination,
            limit=limit,
            namespace_filter=namespace_filter,
            agent_id=agent_id,
            task_id=task_id,
            tags=tags,
            path_prefix=path_prefix,
        )
        executed_scouts: set[str] = set()
        all_candidates: list[Candidate] = []

        # scout_task_context only participates when a task_id was supplied.
        phase_a = [
            spec
            for spec in SCOUT_REGISTRY
            if spec.phase == "A" and (not spec.requires_task_id or task_id is not None)
        ]
        _phase_a_start = time.perf_counter()
        phase_a_results = await asyncio.gather(
            *(spec.run(base_ctx) for spec in phase_a), return_exceptions=True
        )
        _phase_a_elapsed = time.perf_counter() - _phase_a_start

        # Phase A scouts run concurrently, so they share one wall-clock
        # (_phase_a_elapsed): each scout's telemetry records its *share* of that
        # duration, not an individual latency (only Phase B latencies are per-scout).
        for spec, result in zip(phase_a, phase_a_results, strict=True):
            if isinstance(result, BaseException):
                _log_scout_failure(spec.name, result, failed_scouts=failed_scout_names)
                continue
            _record_scout_success(
                spec.name,
                result,
                _phase_a_elapsed * 1000,
                all_candidates=all_candidates,
                executed_scouts=executed_scouts,
            )

        # ── Phase A normalisation for provenance seeding ──────────
        phase_a_normalised = merge_and_normalize(all_candidates)
        phase_a_normalised.sort(key=lambda c: c.score, reverse=True)

        logger.info(
            "run_retrieve: phase A complete",
            extra={
                "receipt_id": receipt_id,
                "scouts_fired": len(executed_scouts),
                "raw_candidates": len(all_candidates),
                "normalised_candidates": len(phase_a_normalised),
            },
        )

        # ── Phase B: sequential scouts seeded from Phase A ─────────
        seed_ids = [c.node_id for c in phase_a_normalised[:max_context_nodes]]
        logger.debug(
            "run_retrieve: phase B seeding",
            extra={"receipt_id": receipt_id, "seed_count": len(seed_ids)},
        )
        if seed_ids:
            seeded_ctx = dataclasses.replace(base_ctx, seed_ids=seed_ids)
            for spec in (s for s in SCOUT_REGISTRY if s.phase == "B"):
                _t = time.perf_counter()
                try:
                    result = await spec.run(seeded_ctx)
                except Exception as exc:
                    _log_scout_failure(spec.name, exc, failed_scouts=failed_scout_names)
                    continue
                _record_scout_success(
                    spec.name,
                    result,
                    (time.perf_counter() - _t) * 1000,
                    all_candidates=all_candidates,
                    executed_scouts=executed_scouts,
                )

        # Contradictions — a conflict producer (not a candidate scout), fired only
        # when surface_conflicts is set. Recorded in executed_scouts so it shows up
        # in the scouts_fired audit trail (it was previously never recorded).
        if surface_conflicts:
            try:
                conflicts_found = await scout_contradictions(
                    seed_ids,
                    projection,
                    knowledge,
                    namespace_filter=namespace_filter,
                    agent_id=agent_id,
                    task_id=task_id,
                )
                executed_scouts.add(SCOUT_CONTRADICTIONS)
                logger.debug(
                    "run_retrieve: contradictions surfaced",
                    extra={"conflict_count": len(conflicts_found)},
                )
            except Exception as exc:
                _log_scout_failure(SCOUT_CONTRADICTIONS, exc, failed_scouts=failed_scout_names)

        # Record scouts_fired using canonical names in order. A scout appears here
        # iff it executed without raising — empty result sets still count as "fired"
        # so the audit trail accurately reflects what the pipeline did. failed_scouts
        # is the disjoint set that ran and raised (degraded-mode signal).
        scouts_fired = [s for s in ALL_SCOUT_NAMES if s in executed_scouts]
        failed_scouts = [s for s in ALL_SCOUT_NAMES if s in failed_scout_names]

        # ── Merge & Normalise all candidates ──────────────────────
        merged = merge_and_normalize(all_candidates)
        candidates_considered = len(merged)

        # ── Terrace 1: rerank_fast ────────────────────────────────
        # ── Pre-fetch salience + usage maps for reranking ──
        # get_node_stats_batch returns full rows (SELECT *), so the usage counters
        # (retrieval_count, last_used_at/last_retrieved_at) ride along for free — the
        # usage signal costs no extra query.
        all_node_ids = [c.node_id for c in merged]
        stats_batch = await stats_store.get_node_stats_batch(all_node_ids)
        now = datetime.now(UTC)
        salience_map: dict[str, float] = {}
        usage_map: dict[str, float] = {}
        for nid in all_node_ids:
            stats = stats_batch.get(nid)
            raw = stats["salience"] if stats else DEFAULT_SALIENCE
            salience_map[nid] = raw if isinstance(raw, float) else DEFAULT_SALIENCE
            usage_map[nid] = _usage_from_stats(stats, now, lcma_config)

        reranked = _rerank_fast(
            merged, lcma_config, knowledge, salience_map=salience_map, usage_map=usage_map
        )
        terrace_reached = 1

        logger.info(
            "run_retrieve: reranking complete",
            extra={
                "receipt_id": receipt_id,
                "candidates_considered": candidates_considered,
                "scouts_fired": scouts_fired,
                "temperature": temperature,
            },
        )

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
                # Tantivy indexes ``doc.full_content`` (title prepended as
                # H1), so snippet against the same string to match the
                # ``lithos_search`` behaviour for title-only query matches.
                snippet = generate_snippet(doc.full_content, query)
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
                        "salience": salience_map.get(c.node_id, DEFAULT_SALIENCE),
                        "usage_score": usage_map.get(c.node_id, 0.0),
                    }
                )
            except FileNotFoundError:
                logger.warning("Document %s not found during result building", c.node_id)
                continue

        logger.info(
            "run_retrieve: completed",
            extra={
                "receipt_id": receipt_id,
                "result_count": len(results),
                "limit": limit,
                "agent_id": agent_id,
                "task_id": task_id,
            },
        )

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
                    logger.warning(
                        "run_retrieve: working memory upsert failed",
                        extra={"node_id": r["id"], "task_id": task_id, "receipt_id": receipt_id},
                        exc_info=True,
                    )

        envelope: dict[str, object] = {
            "results": results,
            "temperature": temperature,
            "terrace_reached": terrace_reached,
            "receipt_id": receipt_id,
            # Degraded-mode signal: which scouts ran and raised (empty normally).
            # A caller can tell partial results from a bad backend apart from a
            # genuinely empty corpus. Always present for a stable envelope shape.
            "degraded": bool(failed_scouts),
            "failed_scouts": failed_scouts,
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
                logger.debug(
                    "run_retrieve: coactivation updated",
                    extra={
                        "receipt_id": receipt_id,
                        "node_count": len(final_node_ids),
                        "pair_count": len(pairs),
                        "namespace": dom_ns,
                    },
                )
            except Exception:
                logger.warning(
                    "run_retrieve: coactivation/node_stats update failed",
                    extra={"receipt_id": receipt_id},
                    exc_info=True,
                )
