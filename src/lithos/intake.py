"""Corpus intake — controlled entry point for Corpus mutations from agent tools.

The intake owns the five-step mutation pipeline that previously lived inline in
the MCP handlers (``lithos_write`` and ``lithos_delete``):

    1. ensure the agent is registered with CoordinationService;
    2. apply the mutation through KnowledgeManager (where ``_write_lock``
       provides atomicity, including ``expected_version`` checks);
    3. synchronise the Search engine (Tantivy + Chroma);
    4. synchronise the link graph (KnowledgeGraph debounces its own flush);
    5. emit the matching ``NOTE_*`` event on the EventBus.

This is the agent-driven counterpart to Reconcile, which is corpus-driven
(see ADR-0001). Intake updates derived views as a write happens; Reconcile
brings them back into agreement after Drift. See ADR-0003 for the design
rationale and rejected alternatives.

Only the delete path is exposed in this module today; ``write`` follows in a
subsequent change. Both ``lithos_write`` and ``lithos_delete`` will eventually
funnel through ``CorpusIntake``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from lithos.coordination import CoordinationService
from lithos.events import NOTE_DELETED, EventBus, LithosEvent
from lithos.graph import KnowledgeGraph
from lithos.knowledge import KnowledgeManager
from lithos.search import SearchEngine
from lithos.telemetry import get_tracer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeleteRequest:
    """Validated input for a Corpus delete."""

    id: str


@dataclass(frozen=True)
class DeleteOutcome:
    """Result of a Corpus delete.

    ``status`` discriminates the case: ``"deleted"`` on success,
    ``"not_found"`` when the id was unknown to the Corpus.
    """

    status: Literal["deleted", "not_found"]
    path: str = ""


class CorpusIntake:
    """The controlled entry point for Corpus mutations from agent tools.

    Construction takes the four collaborators and the event bus. The intake
    holds no state of its own and acquires no lock — ``KnowledgeManager``'s
    internal ``_write_lock`` is the single serialisation point for Corpus
    writes, and the intake must never wrap it.
    """

    def __init__(
        self,
        knowledge: KnowledgeManager,
        search: SearchEngine,
        graph: KnowledgeGraph,
        coordination: CoordinationService,
        event_bus: EventBus,
    ) -> None:
        self._knowledge = knowledge
        self._search = search
        self._graph = graph
        self._coordination = coordination
        self._event_bus = event_bus

    async def delete(self, agent: str, request: DeleteRequest) -> DeleteOutcome:
        """Delete a note from the Corpus and synchronise derived views.

        On success the Search engine has the document removed (awaited via
        ``asyncio.to_thread``) and the link graph has been notified (sync;
        flush debounces). The ``NOTE_DELETED`` event fires only after both
        synchronisations have been kicked off.

        On a search/graph exception the document is already off disk, no
        event is emitted, and the exception propagates to the caller — Drift
        is the corpus-vs-view condition that ``Reconcile`` repairs (ADR-0001).

        Returns ``DeleteOutcome(status="not_found")`` when the id is unknown;
        no view sync, no event, but ``ensure_agent_known`` has still run.
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.intake.delete") as span:
            span.set_attribute("lithos.intake.op", "delete")
            span.set_attribute("lithos.agent", agent)
            span.set_attribute("lithos.id", request.id)

            await self._coordination.ensure_agent_known(agent)

            success, path = await self._knowledge.delete(request.id)
            if not success:
                return DeleteOutcome(status="not_found")

            await asyncio.to_thread(self._search.remove, request.id)
            self._graph.remove_document(request.id)

            await self._emit(
                LithosEvent(
                    type=NOTE_DELETED,
                    agent=agent,
                    payload={"id": request.id, "path": path},
                )
            )

            return DeleteOutcome(status="deleted", path=path)

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating.

        Mirrors ``LithosServer._emit``: a failed event delivery never undoes
        a successful Corpus mutation.
        """
        try:
            await self._event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)
