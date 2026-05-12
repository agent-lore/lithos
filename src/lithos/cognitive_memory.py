"""CognitiveMemory — agent-facing surface of LCMA as a Module facade.

See docs/adr/0005-cognitive-memory-module.md.

This slice (issue #255) lands the seam and lifecycle ordering only.
``CognitiveMemory`` owns the ``StatsStore`` and the ``EnrichWorker``;
``LithosServer.initialize()`` constructs the Module after the projection
is ready and ``LithosServer.shutdown()`` stops it first. Public read /
write methods (retrieve, edge_*, reinforce_*, etc.) migrate in
subsequent slices (#257-#260); existing MCP tool handlers continue to
call LCMA internals directly through server-level aliases.
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
