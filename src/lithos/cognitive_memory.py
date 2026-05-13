"""CognitiveMemory — agent-facing surface of LCMA as a Module facade.

See docs/adr/0005-cognitive-memory-module.md.

Issue #255 landed the seam and lifecycle ordering. Issue #257 migrated
``retrieve`` into the Module; the ``lithos_retrieve`` MCP handler in
``LithosServer`` is now a one-line envelope wrapper. Issue #258 lands
:meth:`node_stats` and :meth:`edge_upsert` / :meth:`edge_list` /
:meth:`edge_delete`; the corresponding MCP handlers are now one-line
wrappers. The remaining methods (``reinforce_*``, ``cache_lookup``,
``conflict_resolve``) migrate in subsequent slices (#259-#260); their
MCP handlers continue to call LCMA internals directly through
server-level aliases until then.

Method ordering convention: lifecycle methods first (``create``,
``attach_coordination``, ``start``, ``stop``), then public agent-facing
methods grouped by domain (retrieve / edges / reinforcement / stats).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lithos.events import EDGE_UPSERTED, LithosEvent
from lithos.lcma.enrich import EnrichWorker
from lithos.lcma.stats import StatsStore

if TYPE_CHECKING:
    from lithos.config import LithosConfig
    from lithos.coordination import CoordinationService
    from lithos.events import EventBus
    from lithos.graph import KnowledgeGraph
    from lithos.knowledge import KnowledgeManager
    from lithos.provenance import ProvenanceProjection
    from lithos.search import SearchEngine

logger = logging.getLogger(__name__)

__all__ = ["CognitiveMemory", "NodeStats"]


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

    The public seam is the six-argument ``create(config, knowledge, search,
    graph, projection, event_bus)`` factory, exactly as ADR-0005 and issue
    #255 specify. The Module owns the ``StatsStore`` lifecycle and the
    ``EnrichWorker``. Public retrieve / edge / reinforcement methods are
    migrated in subsequent slices; this slice exposes only the lifecycle.

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
    ) -> None:
        self._config = config
        self._knowledge = knowledge
        self._search = search
        self._graph = graph
        self._projection = projection
        self._event_bus = event_bus

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
            )
            await self._enrich_worker.start()
        self._started = True

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
        """Upsert an edge and emit :data:`EDGE_UPSERTED`. Returns the edge id.

        Interim shape per ADR-0005: writes call the projection's internal
        ``EdgeStore`` directly. ADR-0006 / issue #263 relocates this to
        ``CorpusIntake.assert_edge`` — keeping the body small (a single
        upsert + the emit) makes that move a one-method swap.
        """
        # Interim: ADR-0005 / #263 will replace this direct edge-store call
        # with ``self._intake.assert_edge(...)`` once CorpusIntake owns
        # assertion. Not a layering bug today.
        edge_id = await self._projection._edge_store.upsert(
            from_id=from_id,
            to_id=to_id,
            edge_type=edge_type,
            weight=weight,
            namespace=namespace,
            provenance_actor=provenance_actor,
            provenance_type=provenance_type,
            evidence=evidence,
            conflict_state=conflict_state,
        )
        try:
            await self._event_bus.emit(
                LithosEvent(
                    type=EDGE_UPSERTED,
                    payload={
                        "edge_id": edge_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "type": edge_type,
                        "namespace": namespace,
                    },
                )
            )
        except Exception:
            logger.exception("Failed to emit %s event", EDGE_UPSERTED)
        return edge_id

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
