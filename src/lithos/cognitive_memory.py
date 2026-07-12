"""CognitiveMemory — agent-facing surface of LCMA as a Module facade.

See docs/adr/0005-cognitive-memory-module.md.

Issue #255 landed the seam and lifecycle ordering. Issue #257 migrated
``retrieve`` into the Module; #258 added ``node_stats`` and the
``edge_*`` methods; #259 added ``reinforce_*``; #260 adds
``cache_lookup`` and ``conflict_resolve``. The corresponding MCP
handlers in ``LithosServer`` are all one-line envelope wrappers
around these methods.

Method ordering convention: lifecycle methods first (``create``,
``attach_coordination``, ``start``, ``stop``), then public agent-facing
methods grouped by domain (retrieve / edges / reinforcement /
cache + conflict resolve).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lithos.errors import SearchBackendError
from lithos.events import EDGE_UPSERTED, LithosEvent
from lithos.intake import EdgeRequest, NoteUpdateRequest
from lithos.knowledge import _normalize_datetime
from lithos.lcma.enrich import ENRICH_AGENT, EnrichWorker

# Re-exported for callers outside the lcma boundary (ADR-0005): the CLI
# extract-entities command shares the enrichment worker's extractor (#313).
from lithos.lcma.entities import ENTITY_EXTRACTOR_VERSION, extract_entities
from lithos.lcma.stats import StatsStore
from lithos.telemetry import get_tracer, lithos_metrics

if TYPE_CHECKING:
    from lithos.config import LithosConfig
    from lithos.coordination import CoordinationService
    from lithos.events import EventBus
    from lithos.graph import KnowledgeGraph
    from lithos.intake import CorpusIntake
    from lithos.knowledge import KnowledgeManager
    from lithos.provenance import ProvenanceProjection
    from lithos.search import SearchEngine

logger = logging.getLogger(__name__)

__all__ = ["ENTITY_EXTRACTOR_VERSION", "CognitiveMemory", "NodeStats", "extract_entities"]


# Default values for ``node_stats`` columns. Used both when a node is
# known to the corpus but has no row in ``node_stats`` yet, and as the
# column-default backstop when shaping a row pulled from the table.
# Keeping the canonical defaults next to the dataclass guarantees the
# wire shape returned by :meth:`CognitiveMemory.node_stats` matches the
# legacy MCP handler exactly.
_NODE_STATS_DEFAULTS: dict[str, Any] = {
    "salience": 0.5,
    "retrieval_count": 0,
    "cited_count": 0,
    "last_retrieved_at": None,
    "last_used_at": None,
    "ignored_count": 0,
    "misleading_count": 0,
    "decay_rate": 0.0,
    "spaced_rep_strength": 0.0,
    "last_decay_applied_at": None,
}


@dataclass(frozen=True)
class NodeStats:
    """Per-node retrieval / salience stats returned by :meth:`CognitiveMemory.node_stats`."""

    node_id: str
    salience: float
    retrieval_count: int
    cited_count: int
    last_retrieved_at: str | None
    last_used_at: str | None
    ignored_count: int
    misleading_count: int
    decay_rate: float
    spaced_rep_strength: float
    last_decay_applied_at: str | None


def _as_int(value: object, default: int) -> int:
    """Coerce a raw ``object`` cell from ``StatsStore.get_node_stats`` to ``int``."""
    if value is None:
        return default
    return int(value)  # type: ignore[arg-type]


def _as_float(value: object, default: float) -> float:
    """Coerce a raw ``object`` cell from ``StatsStore.get_node_stats`` to ``float``."""
    if value is None:
        return default
    return float(value)  # type: ignore[arg-type]


def _as_optional_str(value: object) -> str | None:
    """Coerce a raw ``object`` cell to ``str`` or ``None`` (nullable timestamp)."""
    if value is None:
        return None
    return str(value)


class CognitiveMemory:
    """Module facade over LCMA's agent-facing surface (ADR-0005).

    The public seam is the seven-argument ``create(config, knowledge, search,
    graph, projection, event_bus, intake)`` factory. ADR-0005 / issue #255
    originally specified the six-argument shape; ADR-0006 Slice 1 (issue
    #263) adds ``CorpusIntake`` so that :meth:`edge_upsert` can route
    through ``intake.assert_edge``. The Module owns the ``StatsStore``
    lifecycle and the ``EnrichWorker``.

    ``EnrichWorker`` requires a ``CoordinationService`` that is not part of
    the Module's logical dependency set. The transitional
    :meth:`attach_coordination` setter accepts it for the worker
    construction step inside :meth:`start`. When ``coordination`` becomes a
    Module concern (per ADR-0005's "Anticipated evolution" section) the
    setter goes away.
    """

    def __init__(
        self,
        config: LithosConfig,
        knowledge: KnowledgeManager,
        search: SearchEngine,
        graph: KnowledgeGraph,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        intake: CorpusIntake,
    ) -> None:
        self._config = config
        self._knowledge = knowledge
        self._search = search
        self._graph = graph
        self._projection = projection
        self._event_bus = event_bus
        self._intake = intake

        # Transitional: see class docstring.
        self._coordination: CoordinationService | None = None

        # Module-internal stores. ``StatsStore`` is constructed eagerly but
        # opened in :meth:`start` per issue #255 ("start() opens the
        # internal StatsStore and starts the EnrichWorker").
        self._stats_store: StatsStore = StatsStore(config)
        self._enrich_worker: EnrichWorker | None = None
        self._started: bool = False

    @classmethod
    async def create(
        cls,
        config: LithosConfig,
        knowledge: KnowledgeManager,
        search: SearchEngine,
        graph: KnowledgeGraph,
        projection: ProvenanceProjection,
        event_bus: EventBus,
        intake: CorpusIntake,
    ) -> CognitiveMemory:
        """Construct the Module eagerly.

        StatsStore open and EnrichWorker start are deferred to
        :meth:`start`. Returning from ``create`` guarantees the Module is
        fully wired but not yet running — callers must invoke
        :meth:`start` before issuing work, and (when LCMA is enabled)
        must call :meth:`attach_coordination` before :meth:`start`.
        """
        return cls(
            config=config,
            knowledge=knowledge,
            search=search,
            graph=graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )

    def attach_coordination(self, coordination: CoordinationService) -> None:
        """Attach the CoordinationService required to build EnrichWorker.

        Transitional: ADR-0005's six-arg ``create`` does not include
        ``coordination``, but ``EnrichWorker.__init__`` requires it. The
        server calls this between :meth:`create` and :meth:`start`.
        Removed when coordination consolidates into the Module per
        ADR-0005's "Anticipated evolution" section.
        """
        self._coordination = coordination

    async def start(self) -> None:
        """Open the StatsStore and start the EnrichWorker.

        Raises ``RuntimeError`` if called twice without an intervening
        :meth:`stop`, or if LCMA is enabled but :meth:`attach_coordination`
        was not called. When ``config.lcma.enabled`` is ``False`` the
        StatsStore is still opened (its receipts / working-memory tables
        are read by un-migrated handlers) but no ``EnrichWorker`` is
        constructed.
        """
        if self._started:
            raise RuntimeError("CognitiveMemory.start() called twice")
        await self._stats_store.open()
        if self._config.lcma.enabled:
            if self._coordination is None:
                raise RuntimeError(
                    "CognitiveMemory.start(): coordination not attached. "
                    "Call attach_coordination(...) before start() when LCMA is enabled."
                )
            self._enrich_worker = EnrichWorker(
                config=self._config.lcma,
                event_bus=self._event_bus,
                stats_store=self._stats_store,
                edge_store=self._projection._edge_store,
                knowledge=self._knowledge,
                coordination=self._coordination,
                intake=self._intake,
            )
            await self._enrich_worker.start()
        self._started = True

    async def run_schema_migrations(self) -> None:
        """Run the LCMA schema migrations against the wired ``KnowledgeManager``.

        The :class:`MigrationRegistry` and ``run_migrations`` helpers are
        package-internal to ``lithos.lcma`` (issue #262). Server callers
        invoke this method instead of importing those symbols directly,
        keeping the lcma boundary locked.
        """
        # Local imports keep the lcma symbols inside this Module's compile
        # graph; nothing outside ``cognitive_memory`` / ``provenance`` /
        # ``lcma/*`` may import them.
        from lithos.lcma.migrations import MigrationRegistry, run_migrations

        registry_path = self._config.storage.lithos_store_path / "migrations" / "registry.json"
        registry = MigrationRegistry(registry_path)
        registry.initialize()
        run_migrations(self._knowledge, registry)

    async def get_receipt(self, receipt_id: str, task_id: str) -> dict[str, object] | None:
        """Fetch an LCMA retrieve receipt by id, scoped to *task_id*.

        Public wrapper over :meth:`StatsStore.get_receipt` so callers don't
        reach into the internal store (issue #262).
        """
        return await self._stats_store.get_receipt(receipt_id, task_id)

    async def get_latest_receipt(self, task_id: str, agent_id: str) -> dict[str, object] | None:
        """Return the most recent retrieve receipt for *task_id* / *agent_id*.

        Public wrapper over :meth:`StatsStore.get_latest_receipt` (issue #262).
        """
        return await self._stats_store.get_latest_receipt(task_id, agent_id)

    async def refresh_cached_counts(self) -> None:
        """Refresh the cached LCMA gauge values from the database.

        Public wrapper over :meth:`StatsStore.refresh_cached_counts` so the
        server can prime the cache before OTEL gauge registration without
        touching the internal store (issue #262).
        """
        await self._stats_store.refresh_cached_counts()

    def get_cached_enrich_queue_depth(self) -> int:
        """Return the cached enrich_queue unprocessed-row count (sync, cheap).

        OTEL observable-gauge callbacks must be synchronous; this method
        forwards to the cached value populated by
        :meth:`refresh_cached_counts` / the EnrichWorker drain loop
        (issue #262).
        """
        return self._stats_store.get_cached_enrich_queue_depth()

    def get_cached_coactivation_pairs(self) -> int:
        """Return the cached coactivation-pair count (sync, cheap)."""
        return self._stats_store.get_cached_coactivation_pairs()

    def get_cached_working_memory_active_tasks(self) -> int:
        """Return the cached active-working-memory-task count (sync, cheap)."""
        return self._stats_store.get_cached_working_memory_active_tasks()

    async def stop(self) -> None:
        """Stop the EnrichWorker and close the StatsStore. Idempotent."""
        if self._enrich_worker is not None:
            await self._enrich_worker.stop()
            self._enrich_worker = None
        await self._stats_store.close()
        self._started = False

    # ------------------------------------------------------------------
    # Retrieve (issue #257)
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        namespace_filter: list[str] | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        surface_conflicts: bool = False,
        max_context_nodes: int | None = None,
        tags: list[str] | None = None,
        path_prefix: str | None = None,
    ) -> dict[str, object]:
        """Run the parallel-terraced-scan retrieve pipeline.

        See ``docs/adr/0005-cognitive-memory-module.md`` and
        ``src/lithos/lcma/retrieve.py`` for the algorithm. This method is the
        public seam — the ``lithos_retrieve`` MCP handler is a thin envelope
        around it.

        Errors:
            - ``ScoutFailure`` is raised inside the orchestrator when a single
              scout's backend fails, then **caught and logged** at one
              documented boundary inside this method. Callers do not see it.
            - ``CognitiveMemoryError`` (other subtypes — e.g. RetrieveTimeout)
              **propagates** to the caller. The MCP envelope translates it to
              an error response.
            - ``RuntimeError`` is raised if called before :meth:`start`.
            - Any other exception (e.g. StatsStore I/O failure on receipt
              write) propagates; they are not part of the retrieve contract.

        Precondition:
            ``config.lcma.enabled`` is True. The ``lcma_disabled``
            short-circuit lives in the MCP handler envelope; this method
            asserts ``self._coordination is not None`` (which ``start()``
            enforces when LCMA is enabled).
        """
        if not self._started:
            raise RuntimeError("CognitiveMemory.retrieve called before start()")
        assert self._coordination is not None, (
            "CognitiveMemory.retrieve requires coordination; the MCP handler's "
            "lcma_disabled short-circuit must run before reaching this method."
        )

        # Local import keeps the orchestrator implementation a leaf module;
        # importing it at module load would create an import cycle through
        # lithos.lcma.scouts → lithos.search/knowledge.
        from lithos.lcma.retrieve import _run_retrieve_impl

        return await _run_retrieve_impl(
            query=query,
            search=self._search,
            knowledge=self._knowledge,
            graph=self._graph,
            coordination=self._coordination,
            edge_store=self._projection._edge_store,
            projection=self._projection,
            stats_store=self._stats_store,
            lcma_config=self._config.lcma,
            limit=limit,
            namespace_filter=namespace_filter,
            agent_id=agent_id,
            task_id=task_id,
            surface_conflicts=surface_conflicts,
            max_context_nodes=max_context_nodes,
            tags=tags,
            path_prefix=path_prefix,
        )

    # ------------------------------------------------------------------
    # Edges + node stats (issue #258)
    # ------------------------------------------------------------------

    async def node_stats(self, node_id: str) -> NodeStats | dict[str, Any]:
        """Return per-node retrieval stats.

        Returns the legacy error envelope ``{"status": "error",
        "code": "doc_not_found", "message": ...}`` when *node_id* is not
        known to the corpus, matching the wire shape today's
        ``lithos_node_stats`` MCP handler returns. Otherwise returns a
        :class:`NodeStats` dataclass — empty/default-valued when the
        ``node_stats`` table has no row for the node, populated from the
        row when it does.
        """
        if self._knowledge.get_cached_meta(node_id) is None:
            return {
                "status": "error",
                "code": "doc_not_found",
                "message": f"Node '{node_id}' not found in knowledge base.",
            }

        row = await self._stats_store.get_node_stats(node_id)
        if row is None:
            return NodeStats(node_id=node_id, **_NODE_STATS_DEFAULTS)
        # ``get_node_stats`` returns ``dict[str, object]`` (raw aiosqlite
        # row); coerce per-field so the dataclass shape stays honest.
        return NodeStats(
            node_id=node_id,
            salience=_as_float(row.get("salience"), 0.5),
            retrieval_count=_as_int(row.get("retrieval_count"), 0),
            cited_count=_as_int(row.get("cited_count"), 0),
            last_retrieved_at=_as_optional_str(row.get("last_retrieved_at")),
            last_used_at=_as_optional_str(row.get("last_used_at")),
            ignored_count=_as_int(row.get("ignored_count"), 0),
            misleading_count=_as_int(row.get("misleading_count"), 0),
            decay_rate=_as_float(row.get("decay_rate"), 0.0),
            spaced_rep_strength=_as_float(row.get("spaced_rep_strength"), 0.0),
            last_decay_applied_at=_as_optional_str(row.get("last_decay_applied_at")),
        )

    async def edge_upsert(
        self,
        *,
        agent: str,
        from_id: str,
        to_id: str,
        edge_type: str,
        weight: float,
        namespace: str,
        provenance_actor: str | None = None,
        provenance_type: str | None = None,
        evidence: str | None = None,
        conflict_state: str | None = None,
    ) -> str:
        """Upsert an asserted edge via ``CorpusIntake.assert_edge``.

        Thin wrapper per ADR-0006 Slice 1 (issue #263). The intake owns
        the agent registration, the atomic upsert, and the ``EDGE_UPSERTED``
        event emission. Returns the freshly-upserted ``edge_id``.
        """
        outcome = await self._intake.assert_edge(
            agent,
            EdgeRequest(
                from_id=from_id,
                to_id=to_id,
                edge_type=edge_type,
                weight=weight,
                namespace=namespace,
                provenance_actor=provenance_actor,
                provenance_type=provenance_type,
                evidence=evidence,
                conflict_state=conflict_state,
            ),
        )
        return outcome.edge_id

    async def edge_list(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        edge_type: str | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """List edges through the projection's public read API (ADR-0004).

        Reads go through ``ProvenanceProjection.list_edges`` so the
        asserted-vs-derived predicate that lives in the projection
        applies uniformly. ADR-0004 designates the projection as the
        public read surface; only writes still reach through to the
        edge store (ADR-0005, see :meth:`edge_upsert` /
        :meth:`edge_delete`).
        """
        return await self._projection.list_edges(
            from_id=from_id,
            to_id=to_id,
            edge_type=edge_type,
            namespace=namespace,
        )

    async def edge_delete(self, *, edge_ids: list[str]) -> int:
        """Delete edges by id. Returns the number of rows deleted.

        No MCP surface today — provided for the four-method shape ADR-0005
        describes and for future asserted-edge retraction work (e.g.
        agent-driven contradictions). Mirrors the interim
        ``self._projection._edge_store.delete_edges`` reach-through that
        the enrich worker still uses; ADR-0005 / future #263 will route
        this through a projection-owned write API once it exists.
        """
        # Interim: direct edge-store call mirrors today's use sites and
        # collapses to a public projection write in ADR-0005's successor
        # issue (#263).
        return await self._projection._edge_store.delete_edges(edge_ids=edge_ids)

    # ------------------------------------------------------------------
    # Reinforcement (issue #259, ADR-0005)
    # ------------------------------------------------------------------
    #
    # All reinforcement state writes flow through the Module: the agent
    # passes *what it knows* (cited / ignored / misleading node ids) and
    # the Module applies the consequences against its owned StatsStore
    # and the projection's EdgeStore. Callers no longer thread stores by
    # hand. ``self._projection._edge_store`` is a private-attribute read
    # matching the precedent set in :meth:`start`; a follow-up issue will
    # promote it to a public ``ProvenanceProjection.edge_store`` property.

    async def reinforce_cited(self, cited_ids: list[str]) -> None:
        """Boost stats for every cited node.

        For each *cited_id*:

        - ``cited_count += 1``
        - ``salience += 0.02``
        - ``spaced_rep_strength += 0.05``
        - ``last_used_at`` is refreshed.
        """
        logger.info(
            "CognitiveMemory.reinforce_cited: reinforcing",
            extra={"node_count": len(cited_ids)},
        )
        for node_id in cited_ids:
            await self._stats_store.increment_cited(node_id)
            await self._stats_store.update_salience(node_id, 0.02)
            await self._stats_store.update_spaced_rep_strength(node_id, 0.05)
            await self._stats_store.update_last_used_at(node_id)
            logger.debug(
                "CognitiveMemory.reinforce_cited: node reinforced",
                extra={"node_id": node_id, "salience_delta": 0.02, "spaced_rep_delta": 0.05},
            )

    async def reinforce_ignored(self, ignored_ids: list[str]) -> None:
        """Decay salience for chronically ignored nodes.

        For each *node_id*:

        - ``ignored_count += 1``
        - If ``ignored_count > 5`` **and** ``ignored_count > cited_count``,
          apply ``salience -= 0.02``.
        """
        logger.info(
            "CognitiveMemory.reinforce_ignored: applying ignored penalties",
            extra={"node_count": len(ignored_ids)},
        )
        for node_id in ignored_ids:
            await self._stats_store.increment_ignored(node_id)
            stats = await self._stats_store.get_node_stats(node_id)
            if stats is not None:
                ignored = stats["ignored_count"]
                cited = stats["cited_count"]
                assert isinstance(ignored, int)
                assert isinstance(cited, int)
                if ignored > 5 and ignored > cited:
                    await self._stats_store.update_salience(node_id, -0.02)
                    logger.debug(
                        "CognitiveMemory.reinforce_ignored: decayed salience",
                        extra={
                            "node_id": node_id,
                            "ignored_count": ignored,
                            "cited_count": cited,
                            "salience_delta": -0.02,
                        },
                    )
            logger.debug(
                "CognitiveMemory.reinforce_ignored: incremented ignored count",
                extra={"node_id": node_id},
            )

    async def reinforce_between(self, cited_ids: list[str]) -> None:
        """Strengthen ``related_to`` edges between every pair of cited nodes.

        For each unique same-namespace pair, the existing ``related_to``
        edge is strengthened by +0.03; if no edge exists, one is created
        with weight 0.5 and ``provenance_type="reinforcement"``. Pairs are
        canonicalised so ``from_id <= to_id`` lexicographically — a single
        edge record per undirected relationship. Cross-namespace pairs
        are silently skipped.
        """
        logger.info(
            "CognitiveMemory.reinforce_between: strengthening edges",
            extra={"cited_count": len(cited_ids)},
        )
        edge_store = self._projection._edge_store

        # Resolve namespace per node from the meta cache.
        ns_map: dict[str, str] = {}
        for nid in cited_ids:
            cached = self._knowledge.get_cached_meta(nid)
            if cached is not None:
                ns_map[nid] = cached.namespace
            else:
                logger.debug(
                    "CognitiveMemory.reinforce_between: node has no cached meta, skipping",
                    extra={"node_id": nid},
                )

        for a, b in itertools.combinations(cited_ids, 2):
            ns_a = ns_map.get(a)
            ns_b = ns_map.get(b)
            if ns_a is None or ns_b is None:
                continue
            if ns_a != ns_b:
                logger.debug(
                    "Skipping cross-namespace pair (%s, %s): %s != %s",
                    a,
                    b,
                    ns_a,
                    ns_b,
                )
                continue

            from_id, to_id = (a, b) if a <= b else (b, a)
            namespace = ns_a

            existing = await edge_store.list_edges(
                from_id=from_id,
                to_id=to_id,
                edge_type="related_to",
                namespace=namespace,
            )

            if existing:
                edge_id = str(existing[0]["edge_id"])
                await edge_store.adjust_weight(edge_id, 0.03)
                logger.debug(
                    "CognitiveMemory.reinforce_between: strengthened existing edge",
                    extra={
                        "edge_id": edge_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "weight_delta": 0.03,
                    },
                )
            else:
                eid = await edge_store.upsert(
                    from_id=from_id,
                    to_id=to_id,
                    edge_type="related_to",
                    weight=0.5,
                    namespace=namespace,
                    provenance_type="reinforcement",
                )
                logger.debug(
                    "CognitiveMemory.reinforce_between: created new related_to edge",
                    extra={
                        "edge_id": eid,
                        "from_id": from_id,
                        "to_id": to_id,
                        "namespace": namespace,
                    },
                )

    async def reinforce_misleading(self, misleading_ids: list[str]) -> None:
        """Penalise misleading nodes and weaken their adjacent edges.

        Folds the two helpers that always fired together against the same
        node set in :meth:`LithosServer._apply_task_feedback`
        (``penalize_misleading`` + ``weaken_edges_for_bad_context``).

        For each *node_id*:

        - ``misleading_count += 1``
        - ``salience -= 0.05``
        - If ``misleading_count >= 3``, set ``status = 'quarantined'``
          via :meth:`KnowledgeManager.update`.

        After per-node penalties, every edge incident to *any* of the
        misleading nodes is weakened by ``-0.05`` exactly once (a shared
        edge between two misleading nodes is not double-counted).
        """
        logger.info(
            "CognitiveMemory.reinforce_misleading: applying misleading penalties",
            extra={"node_count": len(misleading_ids)},
        )
        for node_id in misleading_ids:
            await self._stats_store.increment_misleading(node_id)
            await self._stats_store.update_salience(node_id, -0.05)
            stats = await self._stats_store.get_node_stats(node_id)
            if stats is not None:
                misleading = stats["misleading_count"]
                assert isinstance(misleading, int)
                if misleading >= 3:
                    # Route through intake so the status flip re-indexes
                    # Search/graph and emits NOTE_UPDATED; the direct
                    # KnowledgeManager.update did neither (Drift, task 681ac952).
                    # ENRICH_AGENT stamps the event so the enrich worker drops
                    # it instead of re-enqueuing the quarantined node.
                    await self._intake.note_update(
                        ENRICH_AGENT,
                        NoteUpdateRequest(id=node_id, lcma_status="quarantined"),
                    )
                    logger.info(
                        "CognitiveMemory.reinforce_misleading: node quarantined",
                        extra={"node_id": node_id, "misleading_count": misleading},
                    )
            logger.debug(
                "CognitiveMemory.reinforce_misleading: node penalized",
                extra={"node_id": node_id, "salience_delta": -0.05},
            )

        edge_store = self._projection._edge_store
        seen: set[str] = set()
        for node_id in misleading_ids:
            edges_from = await edge_store.list_edges(from_id=node_id)
            edges_to = await edge_store.list_edges(to_id=node_id)
            for edge in [*edges_from, *edges_to]:
                eid = str(edge["edge_id"])
                if eid in seen:
                    continue
                seen.add(eid)
                await edge_store.adjust_weight(eid, -0.05)
                logger.debug(
                    "CognitiveMemory.reinforce_misleading: weakened edge",
                    extra={"edge_id": eid, "node_id": node_id, "weight_delta": -0.05},
                )
        logger.info(
            "CognitiveMemory.reinforce_misleading: edges weakened",
            extra={"node_count": len(misleading_ids), "edges_weakened": len(seen)},
        )

    # ------------------------------------------------------------------
    # Cache + conflict resolve (issue #260)
    # ------------------------------------------------------------------

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating."""
        try:
            await self._event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)

    async def cache_lookup(
        self,
        query: str,
        *,
        source_url: str | None = None,
        max_age_hours: float | None = None,
        min_confidence: float = 0.5,
        limit: int = 3,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Check if fresh cached knowledge exists before doing expensive research.

        Returns a hit envelope, a stale-reference envelope, or a clean miss.
        Validation errors return ``{"status": "error", "code": ..., "message": ...}``.
        """
        logger.info("lithos_cache_lookup query_len=%d source_url=%s", len(query), source_url)

        # Input validation
        if max_age_hours is not None and max_age_hours <= 0:
            return {
                "status": "error",
                "code": "invalid_input",
                "message": "max_age_hours must be positive.",
            }
        if limit < 1:
            return {
                "status": "error",
                "code": "invalid_input",
                "message": "limit must be >= 1.",
            }
        if not (0.0 <= min_confidence <= 1.0):
            return {
                "status": "error",
                "code": "invalid_input",
                "message": "min_confidence must be between 0.0 and 1.0.",
            }

        _lookup_start = time.perf_counter()
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.cache_lookup") as span:
            span.set_attribute("lithos.tool", "lithos_cache_lookup")
            span.set_attribute("cache.source_url_used", source_url is not None)

            candidates: list[str] = []
            candidates_evaluated = 0

            # Fast path: source_url exact lookup
            if source_url is not None:
                fast_doc = await self._knowledge.find_by_source_url(source_url)
                if fast_doc is not None:
                    # Tag filtering on fast path
                    if tags:
                        doc_tags = fast_doc.metadata.tags
                        if all(t in doc_tags for t in tags):
                            candidates = [fast_doc.id]
                        # else: tag filter failed, fall through to semantic
                    else:
                        candidates = [fast_doc.id]

            # Fallback: semantic search
            if not candidates:
                try:
                    sem_results = await asyncio.to_thread(
                        self._search.semantic_search,
                        query=query,
                        limit=limit,
                        threshold=0.0,
                        tags=tags,
                    )
                    candidates = [r.id for r in sem_results[:limit]]
                except SearchBackendError as exc:
                    span.set_attribute("cache.search_error", True)
                    elapsed_ms = (time.perf_counter() - _lookup_start) * 1000
                    lithos_metrics.cache_lookup_duration.record(elapsed_ms)
                    lithos_metrics.cache_lookups.add(1, {"outcome": "error_search_backend"})
                    return {
                        "status": "error",
                        "code": "search_backend_error",
                        "message": f"Semantic search backend failed: {exc}",
                    }

            # Evaluate candidates
            best_hit = None
            first_stale_id: str | None = None
            now = datetime.now(UTC)
            passing_docs: list[Any] = []

            for doc_id in candidates:
                try:
                    doc, _ = await self._knowledge.read(id=doc_id)
                except (FileNotFoundError, ValueError):
                    continue

                candidates_evaluated += 1
                meta = doc.metadata

                # Skip if below confidence threshold
                if meta.confidence < min_confidence:
                    continue

                # Check staleness (explicit expiry)
                if meta.is_stale:
                    if first_stale_id is None:
                        first_stale_id = doc_id
                    continue

                # Check max_age_hours
                if max_age_hours is not None:
                    updated = _normalize_datetime(meta.updated_at)
                    cutoff = now - timedelta(hours=max_age_hours)
                    if updated < cutoff:
                        if first_stale_id is None:
                            first_stale_id = doc_id
                        continue

                passing_docs.append(doc)

            if passing_docs:
                best_hit = max(passing_docs, key=lambda d: d.metadata.confidence)

            span.set_attribute("cache.candidates_evaluated", candidates_evaluated)

            elapsed_ms = (time.perf_counter() - _lookup_start) * 1000
            lithos_metrics.cache_lookup_duration.record(elapsed_ms)

            if best_hit is not None:
                span.set_attribute("cache.hit", True)
                span.set_attribute("cache.stale_exists", False)
                lithos_metrics.cache_lookups.add(1, {"outcome": "hit"})
                return {
                    "hit": True,
                    "document": {
                        "id": best_hit.id,
                        "title": best_hit.title,
                        "content": best_hit.content,
                        "confidence": best_hit.metadata.confidence,
                        "updated_at": best_hit.metadata.updated_at.isoformat(),
                        "expires_at": (
                            best_hit.metadata.expires_at.isoformat()
                            if best_hit.metadata.expires_at
                            else None
                        ),
                        "tags": best_hit.metadata.tags,
                        "source_url": best_hit.metadata.source_url,
                    },
                    "stale_exists": False,
                    "stale_id": None,
                }
            elif first_stale_id is not None:
                span.set_attribute("cache.hit", False)
                span.set_attribute("cache.stale_exists", True)
                lithos_metrics.cache_lookups.add(1, {"outcome": "miss_stale"})
                return {
                    "hit": False,
                    "document": None,
                    "stale_exists": True,
                    "stale_id": first_stale_id,
                }
            else:
                span.set_attribute("cache.hit", False)
                span.set_attribute("cache.stale_exists", False)
                lithos_metrics.cache_lookups.add(1, {"outcome": "miss_clean"})
                return {
                    "hit": False,
                    "document": None,
                    "stale_exists": False,
                    "stale_id": None,
                }

    async def conflict_resolve(
        self,
        edge_id: str,
        resolution: str,
        resolver: str,
        winner_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a contradiction between two notes.

        Sets ``conflict_state`` on a ``contradicts`` edge and, when superseded,
        records the supersedes link via ``KnowledgeManager.update``.
        """
        logger.info(
            "lithos_conflict_resolve edge_id=%s resolution=%s resolver=%s",
            edge_id,
            resolution,
            resolver,
        )
        tracer = get_tracer()
        # Component-level span: the tool-named span (and the lithos.tool
        # attribute) belongs to the MCP boundary, emitted by @tool_span().
        with tracer.start_as_current_span("lithos.memory.conflict_resolve"):
            valid_resolutions = {"accepted_dual", "superseded", "refuted", "merged"}
            if resolution not in valid_resolutions:
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": (
                        f"Invalid resolution '{resolution}'. "
                        f"Must be one of: {', '.join(sorted(valid_resolutions))}"
                    ),
                }

            edge = await self._projection.get_edge(edge_id)
            if edge is None:
                return {
                    "status": "error",
                    "code": "not_found",
                    "message": f"Edge '{edge_id}' not found",
                }

            if edge["type"] != "contradicts":
                return {
                    "status": "error",
                    "code": "invalid_input",
                    "message": (f"Edge '{edge_id}' is type '{edge['type']}', not 'contradicts'"),
                }

            from_id = str(edge["from_id"])
            to_id = str(edge["to_id"])

            loser_id: str | None = None
            if resolution == "superseded":
                if winner_id is None:
                    return {
                        "status": "error",
                        "code": "invalid_input",
                        "message": "winner_id is required when resolution is 'superseded'",
                    }
                if winner_id not in (from_id, to_id):
                    return {
                        "status": "error",
                        "code": "invalid_input",
                        "message": (
                            f"winner_id '{winner_id}' must be either "
                            f"from_id '{from_id}' or to_id '{to_id}'"
                        ),
                    }
                loser_id = to_id if winner_id == from_id else from_id

            updated = await self._projection._edge_store.update_conflict_resolution(
                edge_id,
                conflict_state=resolution,
                provenance_actor=resolver,
            )

            if not updated:
                return {
                    "status": "error",
                    "code": "update_failed",
                    "message": f"Edge '{edge_id}' could not be updated",
                }

            if resolution == "superseded" and winner_id is not None:
                # Route through intake so recording the supersedes link
                # re-indexes Search/graph and emits NOTE_UPDATED, instead of
                # the bypassing KnowledgeManager.update (Drift, task 681ac952).
                # Attributed to the real resolver — a genuine agent write, so
                # re-enrichment of the winner is legitimate, not a self-loop.
                await self._intake.note_update(
                    resolver,
                    NoteUpdateRequest(id=winner_id, supersedes=loser_id),
                )

            await self._emit(
                LithosEvent(
                    type=EDGE_UPSERTED,
                    payload={
                        "edge_id": edge_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "type": "contradicts",
                        "conflict_state": resolution,
                    },
                )
            )

            return {
                "status": "ok",
                "edge_id": edge_id,
                "conflict_state": resolution,
            }
