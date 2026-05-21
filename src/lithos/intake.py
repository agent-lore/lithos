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

Both ``lithos_write`` and ``lithos_delete`` funnel through
``CorpusIntake``. The handlers reduce to wire-shape validation plus a
single call into the intake.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from lithos.coordination import CoordinationService
from lithos.edge_store import EdgeStore
from lithos.errors import SlugCollisionError
from lithos.events import (
    EDGE_UPSERTED,
    NOTE_CREATED,
    NOTE_DELETED,
    NOTE_UPDATED,
    EventBus,
    LithosEvent,
)
from lithos.graph import KnowledgeGraph
from lithos.knowledge import (
    _UNSET,
    DuplicateInfo,
    KnowledgeDocument,
    KnowledgeManager,
    _UnsetType,
)
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


@dataclass(frozen=True)
class WriteRequest:
    """Validated input for a Corpus write (create or update).

    ``id`` discriminates the case: ``None`` ⇒ create, set ⇒ update.

    All wire-shape decoding (ISO-8601 parsing, enum validation, JSON
    summary shape, ``_UNSET`` translation) happens at the handler. The
    intake receives a request whose fields already match the
    ``KnowledgeManager`` boundary semantics: ``_UNSET`` means preserve,
    ``None`` means clear, a value means set.
    """

    title: str
    content: str
    id: str | None = None
    tags: list[str] | _UnsetType = _UNSET
    confidence: float | _UnsetType = _UNSET
    path: str | None = None
    source_task: str | None | _UnsetType = _UNSET
    source_url: str | None | _UnsetType = _UNSET
    derived_from_ids: list[str] | None | _UnsetType = _UNSET
    expires_at: datetime | None | _UnsetType = _UNSET
    expected_version: int | None = None
    schema_version: int | _UnsetType = _UNSET
    namespace: str | None | _UnsetType = _UNSET
    access_scope: str | None | _UnsetType = _UNSET
    note_type: str | None | _UnsetType = _UNSET
    lcma_status: str | None | _UnsetType = _UNSET
    summaries: dict | None | _UnsetType = _UNSET


@dataclass(frozen=True)
class EdgeRequest:
    """Validated input for an asserted edge upsert (ADR-0006 Slice 1).

    Mirrors the keyword arguments of :meth:`EdgeStore.upsert`. The upsert
    key is ``(from_id, to_id, edge_type, namespace)``; ``weight`` and the
    optional provenance fields are payload columns.
    """

    from_id: str
    to_id: str
    edge_type: str
    weight: float
    namespace: str
    provenance_actor: str | None = None
    provenance_type: str | None = None
    evidence: str | None = None
    conflict_state: str | None = None


@dataclass(frozen=True)
class EdgeOutcome:
    """Result of a Corpus assert_edge.

    ``status`` is currently always ``"ok"`` — the room-to-grow shape
    matches :class:`WriteOutcome`'s style so future variants (e.g.
    namespace mismatch, predicate conflict) can land without changing
    the call site.
    """

    edge_id: str
    status: Literal["ok"] = "ok"


@dataclass(frozen=True)
class WriteOutcome:
    """Result of a Corpus write.

    ``status`` is the canonical outcome code; non-success cases carry
    just the auxiliary fields needed to shape the MCP error envelope.
    """

    status: Literal[
        "created",
        "updated",
        "duplicate",
        "invalid_input",
        "version_conflict",
        "content_too_large",
        "slug_collision",
        "error",
    ]
    document: KnowledgeDocument | None = None
    duplicate_of: DuplicateInfo | None = None
    current_version: int | None = None
    slug_collision_existing_id: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


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
        edge_store: EdgeStore,
    ) -> None:
        self._knowledge = knowledge
        self._search = search
        self._graph = graph
        self._coordination = coordination
        self._event_bus = event_bus
        self._edge_store = edge_store

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

    async def write(self, agent: str, request: WriteRequest) -> WriteOutcome:
        """Create or update a note in the Corpus and synchronise derived views.

        On success the Search engine has been re-indexed (awaited via
        ``asyncio.to_thread``) and the link graph has been notified (sync;
        flush debounces). The ``NOTE_CREATED`` / ``NOTE_UPDATED`` event
        fires only after both synchronisations have been kicked off.

        On a search/graph exception the document is already on disk, no
        event is emitted, and the exception propagates to the caller — Drift
        is the corpus-vs-view condition that ``Reconcile`` repairs (ADR-0001).

        Slug collisions are translated from ``SlugCollisionError`` into a
        ``WriteOutcome(status="slug_collision")``. All other non-success
        ``WriteResult`` statuses (``duplicate``, ``invalid_input``,
        ``version_conflict``, ``content_too_large``, ``error``) are forwarded
        verbatim. ``ensure_agent_known`` runs for every call, including
        rejections — matching the prior handler-level contract.
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.intake.write") as span:
            span.set_attribute("lithos.intake.op", "write")
            span.set_attribute("lithos.agent", agent)
            span.set_attribute("lithos.is_update", request.id is not None)

            await self._coordination.ensure_agent_known(agent)

            max_bytes = self._knowledge.config.storage.max_content_size_bytes
            if len(request.content.encode("utf-8")) > max_bytes:
                return WriteOutcome(
                    status="content_too_large",
                    message=(f"Content exceeds maximum size of {max_bytes} bytes"),
                )

            try:
                if request.id is None:
                    # Create — confidence defaults to 1.0 inside KnowledgeManager.
                    create_tags = None if isinstance(request.tags, _UnsetType) else request.tags
                    create_conf = (
                        1.0 if isinstance(request.confidence, _UnsetType) else request.confidence
                    )
                    create_source = (
                        None if isinstance(request.source_task, _UnsetType) else request.source_task
                    )
                    create_url = (
                        None if isinstance(request.source_url, _UnsetType) else request.source_url
                    )
                    create_derived = (
                        None
                        if isinstance(request.derived_from_ids, _UnsetType)
                        else request.derived_from_ids
                    )
                    create_expires = (
                        None if isinstance(request.expires_at, _UnsetType) else request.expires_at
                    )
                    create_schema = (
                        None
                        if isinstance(request.schema_version, _UnsetType)
                        else request.schema_version
                    )
                    create_ns = (
                        None if isinstance(request.namespace, _UnsetType) else request.namespace
                    )
                    create_scope = (
                        None
                        if isinstance(request.access_scope, _UnsetType)
                        else request.access_scope
                    )
                    create_note = (
                        None if isinstance(request.note_type, _UnsetType) else request.note_type
                    )
                    create_status = (
                        None if isinstance(request.lcma_status, _UnsetType) else request.lcma_status
                    )
                    create_summaries = (
                        None if isinstance(request.summaries, _UnsetType) else request.summaries
                    )
                    result = await self._knowledge.create(
                        title=request.title,
                        content=request.content,
                        agent=agent,
                        tags=create_tags,
                        confidence=create_conf,
                        path=request.path,
                        source=create_source,
                        source_url=create_url,
                        derived_from_ids=create_derived,
                        expires_at=create_expires,
                        schema_version=create_schema,
                        namespace=create_ns,
                        access_scope=create_scope,
                        note_type=create_note,
                        lcma_status=create_status,
                        summaries=create_summaries,
                    )
                else:
                    result = await self._knowledge.update(
                        id=request.id,
                        agent=agent,
                        title=request.title,
                        content=request.content,
                        tags=request.tags,
                        confidence=request.confidence,
                        source_url=request.source_url,
                        derived_from_ids=request.derived_from_ids,
                        expires_at=request.expires_at,
                        expected_version=request.expected_version,
                        source=request.source_task,
                        schema_version=request.schema_version,
                        namespace=request.namespace,
                        access_scope=request.access_scope,
                        note_type=request.note_type,
                        lcma_status=request.lcma_status,
                        summaries=request.summaries,
                    )
            except SlugCollisionError as exc:
                logger.info(
                    "lithos_write slug_collision: agent=%s title=%.120s slug=%s existing_id=%s",
                    agent,
                    request.title,
                    exc.slug,
                    exc.existing_id,
                )
                span.set_attribute("lithos.write_status", "slug_collision")
                return WriteOutcome(
                    status="slug_collision",
                    slug_collision_existing_id=exc.existing_id,
                    message=str(exc),
                )

            if result.status not in ("created", "updated"):
                span.set_attribute("lithos.write_status", result.status)
                return WriteOutcome(
                    status=result.status,
                    document=result.document,
                    duplicate_of=result.duplicate_of,
                    current_version=result.current_version,
                    message=result.message,
                    warnings=list(result.warnings),
                )

            doc = result.document
            assert doc is not None

            # Sync derived views in the order pinned by ADR-0003:
            # 1) await search.index (raises IndexingError on total failure —
            #    propagate; doc is on disk, no event fires);
            # 2) graph.add_document (sync; debounces flush);
            # 3) emit NOTE_CREATED / NOTE_UPDATED only after both have been
            #    kicked off.
            indexable = KnowledgeManager.to_indexable(doc)
            await asyncio.to_thread(self._search.index, indexable)
            self._graph.add_document(doc)

            span.set_attribute("lithos.doc_id", doc.id)
            span.set_attribute("lithos.write_status", result.status)
            span.set_attribute(
                "lithos.provenance.source_count",
                len(doc.metadata.derived_from_ids),
            )
            if result.warnings:
                span.set_attribute("lithos.provenance.warning_count", len(result.warnings))

            await self._emit(
                LithosEvent(
                    type=NOTE_UPDATED if request.id else NOTE_CREATED,
                    agent=agent,
                    payload={"id": doc.id, "title": doc.title, "path": str(doc.path)},
                    tags=list(doc.metadata.tags),
                )
            )

            return WriteOutcome(
                status=result.status,
                document=doc,
                warnings=list(result.warnings),
            )

    async def assert_edge(self, agent: str, request: EdgeRequest) -> EdgeOutcome:
        """Assert an edge into the Corpus (ADR-0006 Slice 1).

        Four-step pipeline modelled on :meth:`delete` / :meth:`write`:

        1. ``ensure_agent_known(agent)`` — unconditional, matches the
           other intake methods. ``lithos_edge_upsert`` previously skipped
           registration when ``provenance_actor`` was None; this method
           always registers so the agent set stays an honest superset of
           every party that has touched the Corpus.
        2. ``EdgeStore.upsert`` — atomic SELECT-then-INSERT/UPDATE on
           the upsert key ``(from_id, to_id, edge_type, namespace)``.
        3. **No view-sync step** — no edge-derived view exists today
           (cf. Search index / KnowledgeGraph for documents).
        4. Emit :data:`EDGE_UPSERTED` via :meth:`_emit` so any event-bus
           failure is logged but never propagates back to the caller.

        Returns :class:`EdgeOutcome` with the freshly-upserted ``edge_id``.
        Concurrent edits are out of scope — the upsert is by natural key
        and ``EdgeRequest`` carries no ``expected_version``.
        """
        tracer = get_tracer()
        with tracer.start_as_current_span("lithos.intake.assert_edge") as span:
            span.set_attribute("lithos.intake.op", "assert_edge")
            span.set_attribute("lithos.agent", agent)
            span.set_attribute("lithos.edge.from_id", request.from_id)
            span.set_attribute("lithos.edge.to_id", request.to_id)
            span.set_attribute("lithos.edge.type", request.edge_type)
            span.set_attribute("lithos.edge.namespace", request.namespace)

            await self._coordination.ensure_agent_known(agent)

            edge_id = await self._edge_store.upsert(
                from_id=request.from_id,
                to_id=request.to_id,
                edge_type=request.edge_type,
                weight=request.weight,
                namespace=request.namespace,
                provenance_actor=request.provenance_actor,
                provenance_type=request.provenance_type,
                evidence=request.evidence,
                conflict_state=request.conflict_state,
            )

            span.set_attribute("lithos.edge.edge_id", edge_id)

            await self._emit(
                LithosEvent(
                    type=EDGE_UPSERTED,
                    agent=agent,
                    payload={
                        "edge_id": edge_id,
                        "from_id": request.from_id,
                        "to_id": request.to_id,
                        "type": request.edge_type,
                        "namespace": request.namespace,
                    },
                )
            )

            return EdgeOutcome(edge_id=edge_id, status="ok")

    async def _emit(self, event: LithosEvent) -> None:
        """Emit an event, logging any failure without propagating.

        Mirrors ``LithosServer._emit``: a failed event delivery never undoes
        a successful Corpus mutation.
        """
        try:
            await self._event_bus.emit(event)
        except Exception:
            logger.exception("Failed to emit %s event", event.type)
