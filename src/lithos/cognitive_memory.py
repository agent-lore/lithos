"""CognitiveMemory — agent-facing surface of LCMA as a Module facade.

See docs/adr/0005-cognitive-memory-module.md.

Issue #255 landed the seam and lifecycle ordering. Issue #257 migrated
``retrieve`` into the Module; the ``lithos_retrieve`` MCP handler in
``LithosServer`` is now a one-line envelope wrapper. The remaining write
methods (edge_*, reinforce_*, etc.) migrate in subsequent slices
(#258-#260); their MCP handlers continue to call LCMA internals directly
through server-level aliases until then.

Method ordering convention: lifecycle methods first (``create``,
``attach_coordination``, ``start``, ``stop``), then public agent-facing
methods grouped by domain (retrieve / edges / reinforcement / stats).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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

__all__ = ["CognitiveMemory"]


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
