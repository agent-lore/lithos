"""LLM-adjudicated typed-edge inference (WS1 slice 1 — the front edge of WS2).

The measured 2026-07 substrate found the typed-edge graph semantically empty: 88% of
edges are weight-≤0.05 mechanical ``related_to``, with only two genuine relation types
across the corpus. This module densifies it: for a freshly created/updated note, take
its top semantic neighbours, cheaply pre-filter, then have the configured LLM
adjudicate a relation type + direction + confidence per pair in a **single** call.
Qualifying judgements are written to ``edges.db`` as ``provenance_type="inferred"``
edges (inferred ≠ asserted — marked and filterable; ``contradicts`` is surfaced by
retrieval, never auto-applied).

Layout mirrors :mod:`lithos.lcma.salience`: pure, trivially-testable functions
(pre-filter, prompt build, response parse) up top; the I/O orchestration lives in
:class:`EdgeInferenceEngine`, driven per-node by the enrich worker's drain loop.

Safety properties (see the WS1 plan):

- **Budget-bounded** — a UTC-day token ceiling (``llm_budget`` ledger in stats.db)
  plus a per-drain-cycle call cap; exhaustion skips inference observably, never
  blocks the rest of enrichment.
- **Idempotent & re-inferable** — ``edge_inference_log`` keyed on ``(node_id,
  doc_version, inference_version)``; edges are written before the log row
  (inferred upserts are idempotent by natural key, so at-least-once is safe).
  A document update, an :data:`INFERENCE_VERSION` bump, or an operator ledger
  purge each genuinely re-adjudicates: candidate pairs already carrying an
  inferred edge are NOT excluded — re-adjudication refreshes their weight,
  rationale, and model attribution in place.
- **Non-destructive** — inferred writes go through
  ``CorpusIntake.assert_inferred_edge`` → ``EdgeStore.upsert_inferred``, which
  atomically refuses to displace any row that is not itself an unconflicted
  inferred edge. Asserted edges, legacy rows, and manually resolved conflicts
  always win; a blocked write is counted, not silently absorbed.
- **Stale-proof** — after the LLM call returns, the focus document's version is
  re-read; if the note changed mid-flight, neither edges nor the op-log row are
  written (spend is still recorded), so the queued newer version re-runs
  against fresh content.
- **Cascade-proof** — edge writes go through ``CorpusIntake.assert_edge`` with
  ``origin=EVENT_ORIGIN_ENRICH`` so the worker drops its own ``edge.upserted``
  events; inference additionally only ever fires on note create/update triggers,
  and the op-log makes any residual re-drain a no-op.
- **Failure-isolated** — an LLM outage completes the enrich item without
  synthesis (no requeue churn); a parse failure records the op-log row so a
  schema-incapable model cannot silently re-burn the budget every drain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lithos.errors import LlmError
from lithos.events import EVENT_ORIGIN_ENRICH, NOTE_CREATED, NOTE_UPDATED
from lithos.intake import EdgeRequest
from lithos.lcma.llm import ChatMessage, LlmClient

if TYPE_CHECKING:
    from lithos.config import LlmConfig
    from lithos.frontmatter_codec import KnowledgeDocument
    from lithos.intake import CorpusIntake
    from lithos.knowledge import KnowledgeManager
    from lithos.lcma.stats import StatsStore
    from lithos.search import SearchEngine
    from lithos.telemetry import _LithosMetrics

_lithos_metrics: _LithosMetrics | None = None
try:
    from lithos.telemetry import lithos_metrics as _lithos_metrics

    _HAS_TELEMETRY = True
except Exception:
    _HAS_TELEMETRY = False

logger = logging.getLogger(__name__)

#: Policy fingerprint for the ``edge_inference_log`` identity triple
#: ``(node_id, doc_version, inference_version)``. BUMP THIS when the prompt
#: schema, relation vocabulary, or parse semantics change — a bump invalidates
#: every prior run so unchanged notes are reprocessed under the new policy.
#: A *model/config* switch deliberately does NOT invalidate (it must not
#: silently re-burn the corpus budget); the operator reset for that is
#: ``StatsStore.purge_edge_inference_log(model=...)``.
INFERENCE_VERSION = 1

#: Relation types the adjudicator may assign (design doc WS2). ``none`` is a valid
#: model answer but never a written edge — the parser drops it with the invalid.
RELATION_VOCAB = frozenset(
    {"supports", "contradicts", "refines", "is_example_of", "depends_on", "analogy_to"}
)

#: Relations with no meaningful direction; endpoints are canonicalized
#: ``from_id <= to_id`` (consolidation precedent) so A/B and B/A dedupe.
SYMMETRIC_RELATIONS = frozenset({"contradicts", "analogy_to"})

_DIRECTIONS = frozenset({"focus_to_candidate", "candidate_to_focus"})

#: Rank-score boosts for entity/tag overlap on top of raw similarity (design §WS2:
#: "cheap-pre-filter (similarity + entity/tag overlap)").
_ENTITY_OVERLAP_BOOST = 0.02
_TAG_OVERLAP_BOOST = 0.01

_SYSTEM_PROMPT = """\
You classify the relationship between a FOCUS note and each CANDIDATE note from a \
shared knowledge base. For each candidate, choose exactly one relation:

- supports: A gives evidence for B
- contradicts: A and B make incompatible claims
- refines: A sharpens, narrows, or corrects a detail of B
- is_example_of: A is a concrete instance of the general idea B
- depends_on: A relies on B being true or in place
- analogy_to: A and B are structurally parallel solutions in different domains
- none: no relation clearly holds (use this freely; most pairs are unrelated)

Reply with ONLY a JSON object, no prose:
{"judgements": [{"candidate": <number>, "relation": "<relation>", \
"direction": "focus_to_candidate" | "candidate_to_focus", \
"confidence": <0.0-1.0>, "rationale": "<max 25 words>"}]}

direction reads left-to-right through the relation: focus_to_candidate means \
"FOCUS <relation> CANDIDATE". Include every candidate exactly once."""


@dataclass(frozen=True)
class NeighbourInfo:
    """A semantic-search hit enriched with the metadata the pre-filter needs."""

    node_id: str
    title: str
    snippet: str
    similarity: float
    namespace: str
    entities: frozenset[str]
    tags: frozenset[str]


@dataclass(frozen=True)
class Adjudication:
    """One validated judgement from the LLM response."""

    candidate_index: int  # 1-based, as numbered in the prompt
    relation: str
    direction: str
    confidence: float
    rationale: str


def prefilter_candidates(
    neighbours: list[NeighbourInfo],
    *,
    focus_id: str,
    focus_namespace: str,
    focus_entities: frozenset[str],
    focus_tags: frozenset[str],
    min_similarity: float,
    max_similarity: float,
    k: int,
) -> list[NeighbourInfo]:
    """Cheap, token-free candidate filter + ranking.

    Drops self, cross-namespace hits, and pairs outside the similarity band
    (too-similar ≈ near-duplicate — ``related_to``'s job; too-distant wastes
    adjudication tokens). Pairs that already carry edges — ``related_to`` or
    previously inferred — are deliberately NOT excluded: typed relations are
    additive semantics, and re-adjudication is the mechanism by which a
    document/policy-version change refreshes existing inferred edges (the
    protected inferred-only upsert makes rewrites safe). Survivors are ranked
    by similarity plus a small entity/tag-overlap boost; the top *k* go to
    the LLM.
    """
    scored: list[tuple[float, NeighbourInfo]] = []
    for n in neighbours:
        if n.node_id == focus_id:
            continue
        if n.namespace != focus_namespace:
            continue
        if not (min_similarity <= n.similarity <= max_similarity):
            continue
        score = (
            n.similarity
            + _ENTITY_OVERLAP_BOOST * len(focus_entities & n.entities)
            + _TAG_OVERLAP_BOOST * len(focus_tags & n.tags)
        )
        scored.append((score, n))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [n for _, n in scored[:k]]


def build_adjudication_prompt(
    focus_title: str,
    focus_content: str,
    candidates: list[NeighbourInfo],
    *,
    snippet_chars: int,
) -> list[ChatMessage]:
    """Build the two-message prompt: fixed system instruction + numbered candidates."""
    lines = [
        "FOCUS NOTE",
        f"Title: {focus_title}",
        f"Content: {focus_content[:snippet_chars]}",
        "",
        "CANDIDATES",
    ]
    for i, c in enumerate(candidates, start=1):
        lines.append(f"[{i}] Title: {c.title}")
        lines.append(f"    Snippet: {c.snippet[:snippet_chars]}")
    return [
        ChatMessage("system", _SYSTEM_PROMPT),
        ChatMessage("user", "\n".join(lines)),
    ]


def parse_adjudications(text: str, n_candidates: int) -> tuple[list[Adjudication], int] | None:
    """Defensively parse the model's JSON; ``None`` means wholesale failure.

    Returns ``(adjudications, problems)``. *problems* counts response-contract
    violations that still allowed partial use: invalid entries, duplicate
    candidate indexes (first judgement wins), and candidates the response
    never covered — the prompt demands every candidate exactly once, and a
    ``none`` answer covers its candidate without producing an edge. The caller
    reports ``problems > 0`` observably (a "partial" outcome) instead of
    silently treating malformed output as success.

    Strictness rules (review round 1):

    - candidate indexes must be genuine non-boolean integers — ``1.9`` is a
      contract violation, never candidate 1;
    - direction is never invented: a directional relation missing a valid
      direction is dropped (an inverted edge is worse than no edge);
      symmetric relations ignore direction entirely.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return None

    entries = payload.get("judgements") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return None

    out: list[Adjudication] = []
    problems = 0
    covered: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            problems += 1
            continue
        index = entry.get("candidate")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not (1 <= index <= n_candidates)
        ):
            problems += 1
            continue
        if index in covered:
            problems += 1  # duplicate judgement for one candidate
            continue
        relation = entry.get("relation")
        if relation == "none":
            covered.add(index)  # a valid answer, just not an edge
            continue
        if relation not in RELATION_VOCAB:
            problems += 1
            continue
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            problems += 1
            continue
        if not (0.0 <= confidence <= 1.0):
            problems += 1
            continue
        direction = entry.get("direction")
        if relation in SYMMETRIC_RELATIONS:
            direction = "focus_to_candidate"  # placeholder; endpoints canonicalize
        elif direction not in _DIRECTIONS:
            problems += 1
            continue
        covered.add(index)
        rationale = str(entry.get("rationale", ""))[:300]
        out.append(Adjudication(index, str(relation), str(direction), confidence, rationale))
    problems += n_candidates - len(covered)
    return out, problems


def edge_endpoints(focus_id: str, candidate_id: str, adjudication: Adjudication) -> tuple[str, str]:
    """Resolve ``(from_id, to_id)`` from the judged direction.

    Symmetric relations canonicalize to ``from_id <= to_id`` so the natural
    upsert key dedupes A→B and B→A.
    """
    if adjudication.relation in SYMMETRIC_RELATIONS:
        return (focus_id, candidate_id) if focus_id <= candidate_id else (candidate_id, focus_id)
    if adjudication.direction == "candidate_to_focus":
        return candidate_id, focus_id
    return focus_id, candidate_id


class EdgeInferenceEngine:
    """Per-node orchestration: gates → neighbours → one LLM call → inferred edges.

    Constructed by :class:`~lithos.lcma.enrich.EnrichWorker` only when
    ``lcma.llm`` is enabled. :meth:`maybe_infer` never raises — every failure
    path logs, counts, and returns so the surrounding enrich item completes.
    """

    def __init__(
        self,
        *,
        config: LlmConfig,
        client: LlmClient,
        stats_store: StatsStore,
        knowledge: KnowledgeManager,
        search: SearchEngine,
        intake: CorpusIntake,
        agent: str,
    ) -> None:
        self._config = config
        self._client = client
        self._stats_store = stats_store
        self._knowledge = knowledge
        self._search = search
        self._intake = intake
        self._agent = agent
        self._calls_this_drain = 0

    def reset_drain_counter(self) -> None:
        """Called by the worker at the top of each drain cycle."""
        self._calls_this_drain = 0

    async def close(self) -> None:
        await self._client.close()

    async def maybe_infer(self, node_id: str, trigger_types: object) -> None:
        """Run typed-edge inference for *node_id* if every gate passes."""
        try:
            await self._maybe_infer(node_id, trigger_types)
        except Exception:
            # Inference is strictly additive; never let it fail the enrich item.
            logger.exception("Edge inference failed unexpectedly for %s", node_id)

    async def _maybe_infer(self, node_id: str, trigger_types: object) -> None:
        if not isinstance(trigger_types, (list, set, tuple)) or not (
            NOTE_CREATED in trigger_types or NOTE_UPDATED in trigger_types
        ):
            return  # cascade guard #2: edge-triggered drains never infer
        if not self._knowledge.has_document(node_id):
            return

        try:
            doc, _ = await self._knowledge.read(id=node_id)
        except FileNotFoundError:
            return
        version = doc.metadata.version

        if await self._stats_store.has_edge_inference(node_id, version, INFERENCE_VERSION):
            self._skip("op_log")
            return
        if self._calls_this_drain >= self._config.max_calls_per_drain:
            self._skip("call_cap")
            return
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        tokens_spent, _calls = await self._stats_store.get_llm_spend(day)
        if tokens_spent >= self._config.daily_token_budget:
            self._skip("budget")
            return

        candidates = await self._gather_candidates(node_id, doc)
        if not candidates:
            # Neighbourhood evaluated and found empty at this content version —
            # no token spend, no re-check until the note changes.
            await self._stats_store.record_edge_inference(
                node_id, version, INFERENCE_VERSION, model=self._config.model, edges_written=0
            )
            self._skip("no_candidates")
            return

        self._calls_this_drain += 1
        messages = build_adjudication_prompt(
            doc.title, doc.content, candidates, snippet_chars=self._config.snippet_chars
        )
        started = time.monotonic()
        try:
            result = await self._client.chat(messages)
        except LlmError as exc:
            # Outage semantics: complete the item without synthesis; the op-log
            # stays absent so the node's next create/update retries.
            logger.warning("LLM adjudication failed for %s: %s", node_id, exc)
            self._count_call("error", started)
            return

        await self._stats_store.record_llm_spend(day, result.total_tokens)
        if _HAS_TELEMETRY and _lithos_metrics is not None:
            _lithos_metrics.lcma_llm_tokens.add(
                result.total_tokens, {"estimated": str(result.estimated).lower()}
            )

        parsed = parse_adjudications(result.text, len(candidates))
        if parsed is None:
            # Tokens were genuinely spent — record the op-log row so a
            # schema-incapable model can't re-burn the budget every drain.
            logger.warning(
                "Unparseable LLM adjudication for %s (model=%s)", node_id, self._config.model
            )
            await self._stats_store.record_edge_inference(
                node_id, version, INFERENCE_VERSION, model=self._config.model, edges_written=0
            )
            self._count_call("parse_error", started)
            return
        adjudications, problems = parsed
        if problems:
            logger.warning(
                "LLM adjudication for %s violated the response contract "
                "(%d bad/duplicate/missing entries; using %d valid judgements)",
                node_id,
                problems,
                len(adjudications),
            )

        # Staleness guard: the note may have changed during the (long) LLM
        # call. Spend is already recorded — the tokens are burned either way —
        # but stale judgements must not become edges, and no op-log row is
        # written so the queued newer version re-runs against fresh content.
        if await self._current_version(node_id) != version:
            self._skip("stale_version")
            self._count_call("stale", started)
            return

        namespace = self._namespace_of(node_id)
        written = 0
        blocked = 0
        for adjudication in adjudications:
            if adjudication.confidence < self._config.confidence_floor:
                continue
            candidate = candidates[adjudication.candidate_index - 1]
            from_id, to_id = edge_endpoints(node_id, candidate.node_id, adjudication)
            outcome = await self._intake.assert_inferred_edge(
                self._agent,
                EdgeRequest(
                    from_id=from_id,
                    to_id=to_id,
                    edge_type=adjudication.relation,
                    weight=adjudication.confidence,
                    namespace=namespace,
                    provenance_actor=self._agent,
                    provenance_type="inferred",
                    evidence=json.dumps(
                        {
                            "rationale": adjudication.rationale,
                            "model": self._config.model,
                            "confidence": adjudication.confidence,
                        }
                    ),
                ),
                origin=EVENT_ORIGIN_ENRICH,
            )
            if outcome is None:
                # An asserted / resolved / legacy row holds this key — the
                # judgement is discarded, observably. Inferred never wins.
                blocked += 1
                self._skip("asserted_exists")
                continue
            written += 1
            if _HAS_TELEMETRY and _lithos_metrics is not None:
                _lithos_metrics.lcma_inferred_edges_written.add(
                    1, {"relation": adjudication.relation}
                )

        await self._stats_store.record_edge_inference(
            node_id, version, INFERENCE_VERSION, model=self._config.model, edges_written=written
        )
        self._count_call("partial" if problems else "ok", started)
        logger.info(
            "Edge inference for %s: %d/%d judgements written, %d blocked (version=%d)",
            node_id,
            written,
            len(adjudications),
            blocked,
            version,
        )

    async def _gather_candidates(self, node_id: str, doc: KnowledgeDocument) -> list[NeighbourInfo]:
        """Semantic k-NN → NeighbourInfo (cached meta) → pre-filter.

        ``threshold`` is passed explicitly so the LCMA similarity band is
        self-contained: without it, the global ``SearchConfig.semantic_threshold``
        would silently pre-narrow the configured ``min_similarity`` floor.
        """
        focus_meta = self._knowledge.get_cached_meta(node_id)
        if focus_meta is None:
            return []

        results = await asyncio.to_thread(
            self._search.semantic_search,
            query=f"{doc.title}\n{doc.content[:1000]}",
            limit=self._config.neighbour_k * 3,
            threshold=self._config.min_similarity,
        )

        neighbours: list[NeighbourInfo] = []
        for r in results:
            meta = self._knowledge.get_cached_meta(r.id)
            if meta is None:
                continue
            neighbours.append(
                NeighbourInfo(
                    node_id=r.id,
                    title=r.title,
                    snippet=r.snippet,
                    similarity=r.similarity,
                    namespace=meta.namespace,
                    entities=frozenset(meta.entities),
                    tags=frozenset(meta.tags),
                )
            )

        return prefilter_candidates(
            neighbours,
            focus_id=node_id,
            focus_namespace=focus_meta.namespace,
            focus_entities=frozenset(focus_meta.entities),
            focus_tags=frozenset(focus_meta.tags),
            min_similarity=self._config.min_similarity,
            max_similarity=self._config.max_similarity,
            k=self._config.neighbour_k,
        )

    async def _current_version(self, node_id: str) -> int | None:
        """Fresh read of the focus document's version; ``None`` if it vanished."""
        try:
            doc, _ = await self._knowledge.read(id=node_id)
        except FileNotFoundError:
            return None
        return doc.metadata.version

    def _namespace_of(self, node_id: str) -> str:
        meta = self._knowledge.get_cached_meta(node_id)
        return meta.namespace if meta is not None else "default"

    def _skip(self, reason: str) -> None:
        if _HAS_TELEMETRY and _lithos_metrics is not None:
            _lithos_metrics.lcma_edge_inference_skips.add(1, {"reason": reason})
        logger.debug("Edge inference skipped (%s)", reason)

    def _count_call(self, outcome: str, started: float) -> None:
        if _HAS_TELEMETRY and _lithos_metrics is not None:
            _lithos_metrics.lcma_llm_calls.add(1, {"outcome": outcome})
            _lithos_metrics.lcma_llm_call_duration.record(
                (time.monotonic() - started) * 1000.0, {"outcome": outcome}
            )
