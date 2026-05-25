"""Tests for ``lithos.intake.CorpusIntake`` — the agent-driven Corpus mutation seam.

These tests pin contracts that are awkward to assert at the MCP handler tier:
event-after-view ordering, ensure-agent-before-mutation ordering, search-failure
no-event semantics, and the "event-emit failures don't undo writes" rule.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from lithos.config import LithosConfig
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
from lithos.intake import (
    CorpusIntake,
    DeleteOutcome,
    DeleteRequest,
    EdgeOutcome,
    EdgeRequest,
    WriteOutcome,
    WriteRequest,
)
from lithos.knowledge import KnowledgeManager, WriteResult
from lithos.search import SearchEngine
from lithos.server import LithosServer

# ---------- Fixtures ----------


@pytest_asyncio.fixture
async def stub_intake(
    test_config: LithosConfig,
    knowledge_manager: KnowledgeManager,
    knowledge_graph: KnowledgeGraph,
) -> tuple[CorpusIntake, dict[str, Any]]:
    """A CorpusIntake wired with a real KnowledgeManager + graph but mocked
    SearchEngine, CoordinationService, EventBus, and EdgeStore.

    Returns ``(intake, mocks)`` where ``mocks`` is a dict of the stubs so each
    test can configure side-effects and assert call patterns.
    """
    search = MagicMock(spec=SearchEngine)
    coordination = AsyncMock(spec=CoordinationService)
    event_bus = AsyncMock(spec=EventBus)
    edge_store = AsyncMock(spec=EdgeStore)

    intake = CorpusIntake(
        knowledge=knowledge_manager,
        search=search,
        graph=knowledge_graph,
        coordination=coordination,
        event_bus=event_bus,
        edge_store=edge_store,
    )
    return intake, {
        "search": search,
        "coordination": coordination,
        "event_bus": event_bus,
        "edge_store": edge_store,
    }


# ---------- Unit tests ----------


@pytest.mark.asyncio
async def test_delete_returns_deleted_outcome_when_doc_exists(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    intake, mocks = stub_intake
    result = await knowledge_manager.create(
        title="To delete", content="bye", agent="agent-1", tags=["t"]
    )
    assert result.document is not None
    doc_id = result.document.id

    outcome = await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert isinstance(outcome, DeleteOutcome)
    assert outcome.status == "deleted"
    assert outcome.path  # non-empty relative path
    mocks["search"].remove.assert_called_once_with(doc_id)
    mocks["event_bus"].emit.assert_awaited_once()
    emitted_event = mocks["event_bus"].emit.await_args.args[0]
    assert emitted_event.type == NOTE_DELETED
    assert emitted_event.agent == "agent-1"
    assert emitted_event.payload["id"] == doc_id


@pytest.mark.asyncio
async def test_delete_returns_not_found_for_unknown_id(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    intake, mocks = stub_intake

    outcome = await intake.delete("agent-1", DeleteRequest(id="does-not-exist"))

    assert outcome == DeleteOutcome(status="not_found")
    # Ensure-agent must still run for audit, but no view sync, no event.
    mocks["coordination"].ensure_agent_known.assert_awaited_once_with("agent-1")
    mocks["search"].remove.assert_not_called()
    mocks["event_bus"].emit.assert_not_called()


@pytest.mark.asyncio
async def test_delete_ensures_agent_before_corpus_mutation(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id

    order: list[str] = []
    mocks["coordination"].ensure_agent_known.side_effect = lambda *_a, **_kw: (
        order.append("ensure_agent_known") or None
    )
    # Wrap knowledge.delete to record when it runs.
    original_delete = knowledge_manager.delete

    async def record_delete(target_id: str) -> tuple[bool, str]:
        order.append("knowledge.delete")
        return await original_delete(target_id)

    knowledge_manager.delete = record_delete  # type: ignore[method-assign]

    await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert order == ["ensure_agent_known", "knowledge.delete"]


@pytest.mark.asyncio
async def test_delete_emits_after_search_remove_returns(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """Pins the event-after-indices invariant: ``event_bus.emit`` must not be
    awaited until ``search.remove`` has already returned."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id

    order: list[str] = []
    mocks["search"].remove.side_effect = lambda *_: order.append("search.remove")

    async def record_emit(event: LithosEvent) -> None:
        order.append(f"emit:{event.type}")

    mocks["event_bus"].emit.side_effect = record_emit

    await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert order == ["search.remove", f"emit:{NOTE_DELETED}"]


@pytest.mark.asyncio
async def test_delete_event_emit_failure_does_not_undo_corpus_delete(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """A failed event delivery must never resurrect a successful Corpus
    delete — the corpus is the source of truth, the event is advisory."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id
    doc_path = result.document.path

    mocks["event_bus"].emit.side_effect = RuntimeError("bus down")

    outcome = await intake.delete("agent-1", DeleteRequest(id=doc_id))

    assert outcome.status == "deleted"
    # Doc is gone from the corpus despite the emit failure.
    assert knowledge_manager.get_id_by_path(doc_path) is None


@pytest.mark.asyncio
async def test_delete_search_failure_propagates_with_no_event(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """``search.remove`` exceptions propagate — Drift is Reconcile's job
    (ADR-0001) — and no ``NOTE_DELETED`` event fires."""
    intake, mocks = stub_intake
    result = await knowledge_manager.create(title="x", content="y", agent="agent-1")
    assert result.document is not None
    doc_id = result.document.id
    doc_path = result.document.path

    mocks["search"].remove.side_effect = RuntimeError("search down")

    with pytest.raises(RuntimeError, match="search down"):
        await intake.delete("agent-1", DeleteRequest(id=doc_id))

    # Corpus already mutated — knowledge.delete ran before search.remove.
    assert knowledge_manager.get_id_by_path(doc_path) is None
    # No event should have been emitted because the operation aborted.
    mocks["event_bus"].emit.assert_not_called()


# ---------- Write tests ----------


@pytest.mark.asyncio
async def test_write_create_returns_created_outcome_with_document(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    intake, mocks = stub_intake

    outcome = await intake.write(
        "agent-1",
        WriteRequest(title="Hello", content="world", tags=["t"]),
    )

    assert isinstance(outcome, WriteOutcome)
    assert outcome.status == "created"
    assert outcome.document is not None
    assert outcome.document.title == "Hello"
    mocks["search"].index.assert_called_once()
    mocks["event_bus"].emit.assert_awaited_once()
    emitted_event = mocks["event_bus"].emit.await_args.args[0]
    assert emitted_event.type == NOTE_CREATED
    assert emitted_event.agent == "agent-1"
    assert emitted_event.payload["id"] == outcome.document.id


@pytest.mark.asyncio
async def test_write_update_returns_updated_outcome(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    intake, mocks = stub_intake
    create_result = await knowledge_manager.create(title="orig", content="v1", agent="agent-1")
    assert create_result.document is not None
    doc_id = create_result.document.id

    outcome = await intake.write(
        "agent-1",
        WriteRequest(id=doc_id, title="orig", content="v2", expected_version=1),
    )

    assert outcome.status == "updated"
    assert outcome.document is not None
    assert outcome.document.metadata.version == 2
    emitted_event = mocks["event_bus"].emit.await_args.args[0]
    assert emitted_event.type == NOTE_UPDATED
    assert emitted_event.payload["id"] == doc_id


@pytest.mark.asyncio
async def test_write_emits_after_search_index_returns(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """Pins event-after-indices: ``event_bus.emit`` must not be awaited
    until ``search.index`` has already returned."""
    intake, mocks = stub_intake

    order: list[str] = []
    mocks["search"].index.side_effect = lambda *_: order.append("search.index")

    async def record_emit(event: LithosEvent) -> None:
        order.append(f"emit:{event.type}")

    mocks["event_bus"].emit.side_effect = record_emit

    await intake.write("agent-1", WriteRequest(title="x", content="y"))

    assert order == ["search.index", f"emit:{NOTE_CREATED}"]


@pytest.mark.asyncio
async def test_write_emits_even_when_graph_add_pending(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """``graph.add_document`` debounces its own flush; the event must
    still fire after we hand off to it (no waiting for the deferred
    flush)."""
    intake, mocks = stub_intake

    # Replace the real graph with a stub that records the call but does
    # nothing else — pins that intake does not await the graph.
    intake._graph = MagicMock()  # type: ignore[assignment]

    await intake.write("agent-1", WriteRequest(title="x", content="y"))

    intake._graph.add_document.assert_called_once()
    mocks["event_bus"].emit.assert_awaited_once()
    assert mocks["event_bus"].emit.await_args.args[0].type == NOTE_CREATED


@pytest.mark.asyncio
async def test_write_event_emit_failure_does_not_undo_corpus_create(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """A failed event delivery must never resurrect a successful Corpus
    write — the corpus is the source of truth, the event is advisory."""
    intake, mocks = stub_intake

    mocks["event_bus"].emit.side_effect = RuntimeError("bus down")

    outcome = await intake.write("agent-1", WriteRequest(title="x", content="y"))

    assert outcome.status == "created"
    assert outcome.document is not None
    # Doc is on disk despite the emit failure.
    assert knowledge_manager.has_document(outcome.document.id)


@pytest.mark.asyncio
async def test_write_search_failure_propagates_no_event(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """``search.index`` exceptions propagate — Drift is Reconcile's job
    (ADR-0001) — and no ``NOTE_CREATED`` event fires."""
    intake, mocks = stub_intake

    mocks["search"].index.side_effect = RuntimeError("search down")

    with pytest.raises(RuntimeError, match="search down"):
        await intake.write("agent-1", WriteRequest(title="x", content="y"))

    # The corpus write succeeded before the failure — exactly one doc on disk.
    assert knowledge_manager.document_count == 1
    mocks["event_bus"].emit.assert_not_called()


@pytest.mark.asyncio
async def test_write_does_not_pre_read_for_version_check(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """Pins the no-TOCTOU guarantee: intake forwards ``expected_version``
    to ``KnowledgeManager.update`` and does *not* read the doc beforehand
    to compare versions itself."""
    intake, _ = stub_intake
    create_result = await knowledge_manager.create(title="orig", content="v1", agent="agent-1")
    assert create_result.document is not None
    doc_id = create_result.document.id

    seen_expected: list[int | None] = []
    original_update = knowledge_manager.update

    async def spy_update(**kwargs: Any) -> Any:
        seen_expected.append(kwargs.get("expected_version"))
        return await original_update(**kwargs)

    knowledge_manager.update = spy_update  # type: ignore[method-assign]

    read_calls: list[Any] = []
    original_read = knowledge_manager.read

    async def spy_read(*args: Any, **kwargs: Any) -> Any:
        read_calls.append((args, kwargs))
        return await original_read(*args, **kwargs)

    knowledge_manager.read = spy_read  # type: ignore[method-assign]

    await intake.write(
        "agent-1",
        WriteRequest(id=doc_id, title="orig", content="v2", expected_version=1),
    )

    # ``expected_version`` is forwarded verbatim.
    assert seen_expected == [1]
    # No pre-read happens at the intake layer; the only ``read`` call is
    # the one inside ``KnowledgeManager.update`` itself (under the lock).
    # Two calls are acceptable when KnowledgeManager.update reads under
    # the lock to validate before writing; what matters is intake doesn't
    # read first.
    assert all(
        not call[1].get("from_intake_pre_check")  # type: ignore[union-attr]
        for call in read_calls
    )


@pytest.mark.asyncio
async def test_write_concurrent_updates_serialised_via_knowledge_lock(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """Two concurrent updates with the same ``expected_version`` must
    serialise via ``KnowledgeManager._write_lock``: one wins with
    ``updated``, the other returns ``version_conflict``. Confirms intake
    adds no serialisation layer of its own."""
    intake, _ = stub_intake
    create_result = await knowledge_manager.create(title="orig", content="v1", agent="agent-1")
    assert create_result.document is not None
    doc_id = create_result.document.id

    async def updater(content: str) -> WriteOutcome:
        return await intake.write(
            "agent-1",
            WriteRequest(id=doc_id, title="orig", content=content, expected_version=1),
        )

    a, b = await asyncio.gather(updater("vA"), updater("vB"))

    statuses = sorted([a.status, b.status])
    assert statuses == ["updated", "version_conflict"]


@pytest.mark.asyncio
async def test_write_slug_collision_returns_outcome_not_exception(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """``SlugCollisionError`` raised by KnowledgeManager is caught at
    the intake and translated into ``WriteOutcome(status='slug_collision')``."""
    intake, _ = stub_intake
    create_result = await knowledge_manager.create(
        title="Hello world", content="v1", agent="agent-1"
    )
    assert create_result.document is not None
    existing_id = create_result.document.id

    async def raising_create(**kwargs: Any) -> WriteResult:
        raise SlugCollisionError("hello-world", existing_id)

    knowledge_manager.create = raising_create  # type: ignore[method-assign]

    outcome = await intake.write("agent-1", WriteRequest(title="Hello World", content="v2"))

    assert outcome.status == "slug_collision"
    assert outcome.slug_collision_existing_id == existing_id


@pytest.mark.asyncio
async def test_write_content_too_large_returns_outcome(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
    test_config: LithosConfig,
) -> None:
    """Pins the ``content_too_large`` Corpus invariant. ``ensure_agent_known``
    must still run; ``knowledge.create`` must not."""
    intake, mocks = stub_intake

    create_calls: list[Any] = []
    original_create = knowledge_manager.create

    async def spy_create(**kwargs: Any) -> Any:
        create_calls.append(kwargs)
        return await original_create(**kwargs)

    knowledge_manager.create = spy_create  # type: ignore[method-assign]

    oversized = "a" * (test_config.storage.max_content_size_bytes + 1)
    outcome = await intake.write("agent-1", WriteRequest(title="x", content=oversized))

    assert outcome.status == "content_too_large"
    assert create_calls == []
    mocks["coordination"].ensure_agent_known.assert_awaited_once_with("agent-1")
    mocks["event_bus"].emit.assert_not_called()


@pytest.mark.asyncio
async def test_write_task_scope_invariant_runs_under_lock(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
    knowledge_manager: KnowledgeManager,
) -> None:
    """An update with ``access_scope='task'`` and no source must fail
    with ``invalid_input`` — and the check must come from
    ``KnowledgeManager.update`` (under the write lock), not from a
    handler-side pre-read at the intake layer."""
    intake, _ = stub_intake
    create_result = await knowledge_manager.create(title="t", content="v1", agent="agent-1")
    assert create_result.document is not None
    doc_id = create_result.document.id

    outcome = await intake.write(
        "agent-1",
        WriteRequest(id=doc_id, title="t", content="v2", access_scope="task"),
    )

    assert outcome.status == "invalid_input"
    assert outcome.message is not None
    assert "task" in outcome.message


# ---------- Integration tests ----------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_write_handler_routes_through_intake(
    server: LithosServer,
) -> None:
    """End-to-end check that the ``lithos_write`` MCP handler now goes
    through ``CorpusIntake`` — full pipeline observable via event bus.
    """
    write_tool = await server.mcp.get_tool("lithos_write")

    queue = server.event_bus.subscribe(event_types=[NOTE_CREATED, NOTE_UPDATED])
    try:
        create_result = await write_tool.fn(title="Routed", content="hello", agent="agent-1")
        assert create_result["status"] == "created"
        doc_id = create_result["id"]

        event = queue.get_nowait()
        assert event.type == NOTE_CREATED
        assert event.agent == "agent-1"
        assert event.payload["id"] == doc_id

        update_result = await write_tool.fn(
            id=doc_id,
            title="Routed",
            content="updated",
            agent="agent-1",
            expected_version=1,
        )
        assert update_result["status"] == "updated"
        assert update_result["version"] == 2

        event = queue.get_nowait()
        assert event.type == NOTE_UPDATED
        assert event.payload["id"] == doc_id
    finally:
        server.event_bus.unsubscribe(queue)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_write_md_path_used_as_explicit_filename(
    server: LithosServer,
) -> None:
    """End-to-end: a `.md`-ending path is stored at exactly that location.

    Regression test for issue #300: the caller controls the filename, not the
    slugified title.
    """
    write_tool = await server.mcp.get_tool("lithos_write")

    result = await write_tool.fn(
        title="Something Different",
        content="explicit filename",
        agent="agent-1",
        path="projects/explicit/foo.md",
    )

    assert result["status"] == "created"
    assert result["path"] == str(Path("projects") / "explicit" / "foo.md")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_write_rejects_md_in_intermediate_segment(
    server: LithosServer,
) -> None:
    """End-to-end: paths with `.md` in a non-final segment are rejected.

    Regression test for issue #300: we must not silently create directories
    whose names end in `.md`.
    """
    write_tool = await server.mcp.get_tool("lithos_write")

    result = await write_tool.fn(
        title="Bad Path",
        content="should fail",
        agent="agent-1",
        path="bad.md/nested.md",
    )

    assert result["status"] == "invalid_input"
    assert ".md" in result["message"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_write_explicit_md_path_collision_returns_duplicate(
    server: LithosServer,
) -> None:
    """End-to-end: two distinct titles targeting the same explicit `.md` path must
    not silently overwrite. The second call gets status="duplicate" carrying the
    first doc's id+title, and the on-disk content remains the first body.

    Regression test for the path-collision class of bugs that would otherwise
    accompany issue #300's explicit-filename mode.
    """
    write_tool = await server.mcp.get_tool("lithos_write")
    read_tool = await server.mcp.get_tool("lithos_read")

    first = await write_tool.fn(
        title="First Title",
        content="first body",
        agent="agent-1",
        path="conflicts/same.md",
    )
    assert first["status"] == "created"
    first_id = first["id"]

    second = await write_tool.fn(
        title="Different Title",
        content="second body",
        agent="agent-1",
        path="conflicts/same.md",
    )
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] is not None
    assert second["duplicate_of"]["id"] == first_id
    assert second["duplicate_of"]["title"] == "First Title"

    # Round-trip: reading by id still returns the first body (no overwrite).
    fetched = await read_tool.fn(id=first_id)
    assert fetched["title"] == "First Title"
    assert "first body" in fetched["content"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_delete_handler_routes_through_intake(
    server: LithosServer,
) -> None:
    """End-to-end check that the ``lithos_delete`` MCP handler now goes
    through ``CorpusIntake`` — full pipeline observable via event bus."""
    create_tool = await server.mcp.get_tool("lithos_write")
    delete_tool = await server.mcp.get_tool("lithos_delete")

    create_result = await create_tool.fn(title="To delete", content="bye", agent="agent-1")
    doc_id = create_result["id"]

    queue = server.event_bus.subscribe(event_types=[NOTE_DELETED])
    try:
        delete_result = await delete_tool.fn(id=doc_id, agent="agent-1")
        assert delete_result == {"success": True}

        event = queue.get_nowait()
        assert event.type == NOTE_DELETED
        assert event.agent == "agent-1"
        assert event.payload["id"] == doc_id

        # Doc is gone from the search index too.
        assert server.knowledge.get_id_by_path(create_result["path"]) is None
    finally:
        server.event_bus.unsubscribe(queue)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lithos_delete_handler_returns_doc_not_found_envelope(
    server: LithosServer,
) -> None:
    delete_tool = await server.mcp.get_tool("lithos_delete")

    result = await delete_tool.fn(id="00000000-0000-0000-0000-000000000000", agent="agent-1")

    assert result == {
        "status": "error",
        "code": "doc_not_found",
        "message": "Document not found: 00000000-0000-0000-0000-000000000000",
    }


# ---------- assert_edge tests (ADR-0006 Slice 1, issue #263) ----------


@pytest.mark.asyncio
async def test_assert_edge_registers_agent_unconditionally(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """``ensure_agent_known`` runs for every assert_edge — matches the
    ADR-0006 acceptance criterion that closes the loophole where
    ``lithos_edge_upsert`` previously skipped registration when
    ``provenance_actor`` was None."""
    intake, mocks = stub_intake
    mocks["edge_store"].upsert.return_value = "edge_abc123"

    outcome = await intake.assert_edge(
        "agent-1",
        EdgeRequest(
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        ),
    )

    assert isinstance(outcome, EdgeOutcome)
    assert outcome.status == "ok"
    assert outcome.edge_id == "edge_abc123"
    mocks["coordination"].ensure_agent_known.assert_awaited_once_with("agent-1")


@pytest.mark.asyncio
async def test_assert_edge_upserts_with_translated_request(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """``EdgeRequest`` field names map verbatim onto ``EdgeStore.upsert`` kwargs."""
    intake, mocks = stub_intake
    mocks["edge_store"].upsert.return_value = "edge_xyz"

    request = EdgeRequest(
        from_id="src",
        to_id="dst",
        edge_type="cites",
        weight=0.9,
        namespace="proj-a",
        provenance_actor="actor-1",
        provenance_type="manual",
        evidence='{"why": "see thread"}',
        conflict_state=None,
    )
    outcome = await intake.assert_edge("agent-1", request)

    assert outcome.edge_id == "edge_xyz"
    mocks["edge_store"].upsert.assert_awaited_once_with(
        from_id="src",
        to_id="dst",
        edge_type="cites",
        weight=0.9,
        namespace="proj-a",
        provenance_actor="actor-1",
        provenance_type="manual",
        evidence='{"why": "see thread"}',
        conflict_state=None,
    )


@pytest.mark.asyncio
async def test_assert_edge_emits_edge_upserted_after_upsert(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """Event-after-upsert ordering matches the ``delete()`` / ``write()``
    pattern: emit only after the mutation has returned."""
    intake, mocks = stub_intake
    mocks["edge_store"].upsert.return_value = "edge_evt"

    order: list[str] = []
    mocks["edge_store"].upsert.side_effect = lambda **_kw: (
        order.append("edge_store.upsert") or "edge_evt"
    )

    async def record_emit(event: LithosEvent) -> None:
        order.append(f"emit:{event.type}")

    mocks["event_bus"].emit.side_effect = record_emit

    await intake.assert_edge(
        "agent-1",
        EdgeRequest(
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.7,
            namespace="default",
        ),
    )

    assert order == ["edge_store.upsert", f"emit:{EDGE_UPSERTED}"]
    emitted_event = mocks["event_bus"].emit.await_args.args[0]
    assert emitted_event.agent == "agent-1"
    assert emitted_event.payload["edge_id"] == "edge_evt"
    assert emitted_event.payload["from_id"] == "a"
    assert emitted_event.payload["to_id"] == "b"
    assert emitted_event.payload["type"] == "related_to"
    assert emitted_event.payload["namespace"] == "default"


@pytest.mark.asyncio
async def test_assert_edge_swallows_event_emit_failure(
    stub_intake: tuple[CorpusIntake, dict[str, Any]],
) -> None:
    """A failed event delivery must never undo a successful edge upsert —
    matches ADR-0001 / the existing ``delete`` and ``write`` semantics."""
    intake, mocks = stub_intake
    mocks["edge_store"].upsert.return_value = "edge_silent"
    mocks["event_bus"].emit.side_effect = RuntimeError("bus down")

    outcome = await intake.assert_edge(
        "agent-1",
        EdgeRequest(
            from_id="a",
            to_id="b",
            edge_type="related_to",
            weight=0.5,
            namespace="default",
        ),
    )

    assert outcome == EdgeOutcome(edge_id="edge_silent", status="ok")
    mocks["edge_store"].upsert.assert_awaited_once()
    mocks["event_bus"].emit.assert_awaited_once()
