"""The one place the Lithos component graph is wired together.

Before this module, three paths built the same graph by hand — ``LithosServer``
(``server.py``), the ``reconcile`` per-scope functions, and half a dozen ``cli``
commands — and they had drifted. The cost was not just duplication: the
"exactly one ``EdgeStore`` writer" rule of ADR-0006 Slice 1 (issue #263) was
unexpressible at any interface, so the CLI and reconcile paths quietly opened a
*second* writer against ``edges.db`` by letting
:meth:`ProvenanceProjection.create` self-create its store.

:func:`build_pipeline` is that missing interface. Construction order and the
one-writer rule live here once, and a constructor signature change stops being a
three-file audit.

Scope: construction, plus the opens construction implies (the embedding model,
the SQLite handles). It deliberately stops short of *lifecycle* — no
``memory.start()``, no schema migrations, no background workers, no OTEL gauge
registration. Those stay with the server, which owns process lifetime. A CLI
command gets the same object graph simply by never starting the workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lithos.cognitive_memory import CognitiveMemory
from lithos.coordination import CoordinationService
from lithos.edge_store import EdgeStore
from lithos.events import EventBus
from lithos.graph import KnowledgeGraph
from lithos.intake import CorpusIntake
from lithos.knowledge import KnowledgeManager
from lithos.provenance import ProvenanceProjection
from lithos.search import SearchEngine

if TYPE_CHECKING:
    from lithos.config import LithosConfig

__all__ = ["Pipeline", "build_pipeline"]


@dataclass(frozen=True)
class Pipeline:
    """A fully-wired, not-yet-running Lithos component graph.

    Frozen because the wiring is an invariant, not state: once built, no caller
    should swap a component out from under the others that captured it.
    """

    config: LithosConfig
    knowledge: KnowledgeManager
    search: SearchEngine
    graph: KnowledgeGraph
    coordination: CoordinationService
    event_bus: EventBus
    edge_store: EdgeStore
    projection: ProvenanceProjection
    intake: CorpusIntake
    memory: CognitiveMemory

    async def aclose(self) -> None:
        """Release the pipeline's own resources. Idempotent.

        Only ``edge_store`` holds a handle this module opened —
        :class:`SearchEngine` and :class:`CoordinationService` expose no close.
        Closing matters most to short-lived CLI runs: leaving the aiosqlite
        worker thread alive is what produced the "Event loop is closed"
        warnings and CI hangs of issue #172.

        Does *not* stop workers — a caller that ran ``memory.start()`` owns the
        matching ``stop()``.
        """
        await self.edge_store.close()


async def build_pipeline(
    config: LithosConfig,
    *,
    knowledge: KnowledgeManager | None = None,
    search: SearchEngine | None = None,
    graph: KnowledgeGraph | None = None,
    coordination: CoordinationService | None = None,
    event_bus: EventBus | None = None,
    edge_store: EdgeStore | None = None,
    projection: ProvenanceProjection | None = None,
    intake: CorpusIntake | None = None,
    memory: CognitiveMemory | None = None,
) -> Pipeline:
    """Build the component graph for *config*, in dependency order.

    Every component may be supplied pre-built, in which case it is adopted
    as-is and its collaborators are wired to it. That is what keeps the server's
    test-injection seam working (tests pre-inject a mock ``search`` to skip the
    real embedding backend) and it is the only reason these keyword arguments
    exist — production callers pass ``config`` alone.

    The order below is load-bearing:

    1. ``ensure_directories`` — the SQLite opens below create files under them.
    2. ``SearchEngine.create`` — async so the embedding model is loaded before
       any caller can observe an unloaded engine; captured by value by the
       intake and by CognitiveMemory, so it must exist first.
    3. **One** ``EdgeStore``, opened here and injected into both the projection
       and the intake. This is the ADR-0006 invariant: the projection owns
       projection-class rows, ``CorpusIntake.assert_edge`` owns asserted-class
       rows, and they share a single SQLite handle so there is exactly one
       writer. Passing ``edge_store=`` to ``ProvenanceProjection.create`` also
       suppresses its self-create-and-open branch.
    4. ``CorpusIntake`` before ``CognitiveMemory`` — the latter declares intake
       as a constructor dependency so ``edge_upsert`` routes through
       ``intake.assert_edge``.
    5. ``attach_coordination`` — a transitional setter (ADR-0005) that
       ``memory.start()`` requires; done here so no caller can forget it.

    Returns a :class:`Pipeline` that is wired but idle. Start workers via
    ``pipeline.memory.start()``; release handles via ``pipeline.aclose()``.
    """
    config.ensure_directories()

    knowledge = knowledge or KnowledgeManager(config)
    graph = graph or KnowledgeGraph(config)
    coordination = coordination or CoordinationService(config)
    event_bus = event_bus or EventBus(config.events)

    if search is None:
        search = await SearchEngine.create(config)

    if edge_store is None:
        edge_store = EdgeStore(config)
        await edge_store.open()
    if projection is None:
        projection = await ProvenanceProjection.create(config, edge_store=edge_store)

    if intake is None:
        intake = CorpusIntake(
            knowledge=knowledge,
            search=search,
            graph=graph,
            coordination=coordination,
            event_bus=event_bus,
            edge_store=edge_store,
        )

    if memory is None:
        memory = await CognitiveMemory.create(
            config=config,
            knowledge=knowledge,
            search=search,
            graph=graph,
            projection=projection,
            event_bus=event_bus,
            intake=intake,
        )
        memory.attach_coordination(coordination)

    await coordination.initialize()

    return Pipeline(
        config=config,
        knowledge=knowledge,
        search=search,
        graph=graph,
        coordination=coordination,
        event_bus=event_bus,
        edge_store=edge_store,
        projection=projection,
        intake=intake,
        memory=memory,
    )
